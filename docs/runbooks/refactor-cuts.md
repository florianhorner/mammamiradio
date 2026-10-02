# Refactor checklist

Use this checklist for behavior-preserving extractions. The owning workspace
remains the only writer. Declare the module boundary and exact write-set before
editing; keep public contracts, privacy gates and audio continuity unchanged.

## Before editing

- Search every use of a moved symbol across source, tests, scripts, workflows
  and documentation. Include string patch targets and dynamic imports.
- Check dependency closure. Move shared primitives deliberately so the new
  module cannot introduce an import cycle or depend on its former owner.
- Read the affected tests and identify the observable invariant they protect.
- Keep further cuts separate unless they are required by this dependency boundary.

## Verify the cut

- Compare moved function bodies against the base with AST source extraction.
  Document each intentional behavior change and its regression coverage.
- Add identity guards for facade re-exports (`facade.X is newmodule.X`). Keep
  intentional re-export lint annotations after removing old definitions.
- Update mocks at the module where a symbol is looked up, not just where it
  was originally defined. Update operational commands and checker inventories.
- Run focused tests, formatting, type checks, dead-code checks and the existing
  per-module coverage floors. Preserve all applicable release gates.
- Complete adversarial, coverage and docs/config consistency review, including
  a new review after fixes or manually resolved integration conflicts.

## Runtime acceptance

When the scoped change requires Edge validation, prepare an explicitly approved
Edge release after the build gate passes. Check `/public-status`, authenticated
`/status`, `/healthz`, `/readyz`, the first `/stream` byte and listener-audible
continuity on the approved test installation. Source checks do not establish
runtime or human listening evidence. Landing and deployment require their own
explicit authorization; never restart a live Home Assistant installation as a
refactor experiment.
