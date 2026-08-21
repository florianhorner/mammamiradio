# 002: allow action-pin bumps inside the contract-drift gate

This proposal changes the *scope of the freeze*, not the v1 wire payload. It
adds no field and touches no fixture, so the payload sections of the template
in `README.md` do not apply and are answered as "none" below.

## Field
None. No wire-visible change. The proposal narrows which diffs to
`.github/workflows/contract-drift.yml` count as a frozen-path violation.

## Why
`.github/workflows/contract-drift.yml` is a frozen path, and Dependabot's
`github-actions` ecosystem bumps action pins *inside* it. Those PRs fail
`Frozen paths require Contract-Change` and can only merge inside a window that
only Florian can open. Two are wedged today: #906 (`actions/checkout`
7.0.0 → 7.0.1) and #905 (`actions/setup-python` 6 → 7). This recurs every time
an action releases.

The freeze exists so nobody quietly weakens the contract gate. A Dependabot
bump of a pinned action's ref does not weaken it — it keeps the gate running on
supported, patched tooling. Right now the rule produces pure friction and, worse,
creates pressure to open contract windows for routine dependency chores. Casual
windows are exactly the failure mode the freeze is meant to prevent.

Scoping the gate per file in Dependabot is not available: the `github-actions`
ecosystem matches `ignore` entries by dependency name, not by path, so excluding
this file would stop `actions/checkout` updates across every workflow in the repo.

## Proposed change
Treat a diff to `.github/workflows/contract-drift.yml` as a frozen-path
violation unless every changed line is the *ref* of an existing
`uses: <action>@<ref>` step.

Still frozen, and still requiring a window:
- adding, removing, or reordering steps or jobs
- changing which action a step uses (the part left of `@`)
- any change to the gate's logic, inputs, permissions, or triggers
- comments and documentation inside the file

## Additive proof
Nothing is removed, renamed, or retyped. The v1 payload, the serializer, the
golden fixture, and the route contract are untouched. The change is a narrowing
of one CI guard's match rule; every diff that is a violation today remains one,
except the single shape described above.

## Fixture diffs (pre-drafted, both repos)
None. No fixture on either side changes.

## Risk and mitigation
The residual risk is a bad ref landing on a legitimate action — a supply-chain
concern, not a contract one. It is bounded by three things already in place: the
action identity itself stays frozen, so a step cannot be repointed at a different
action; CodeQL's `actions` analysis runs on every PR; and the diff is still
visible in review like any other.

## Alternative considered and rejected
Open a contract window each time. Rejected: it is recurring manual toil on a
monthly cadence, and it normalises opening the window for routine chores. The
freeze's value is that it is unconditional — the way to keep it unconditional is
to stop it firing on changes it was never meant to catch.

## Decision needed
Florian: accept or reject the narrowed scope. Implementing it edits
`contract-drift.yml`, which is itself frozen, so it lands inside a window with a
`Contract-Change:` trailer like any other frozen-path change.
