# Festival Mode Feature Train

Status: active integration train  
Train branch: current workspace branch  
Base branch: `origin/main` / GitHub base `main`  
Owner role: this workspace is the convergence branch for Festival Mode slices

## Purpose

This branch is the integration train for Festival Mode. Feature worktrees may build
independent slices against this branch, then hand their diffs back here for review,
integration, validation, and final merge readiness.

Do not rename this branch. Do not use this branch for unrelated cleanup.

## Intake Contract

Each incoming slice must arrive with:

- A short summary of the intended Festival Mode behavior.
- The files changed and the subsystem touched: config/env, scriptwriter, producer,
  admin UI, listener UI, Home Assistant add-on, docs, or tests.
- The validation run in that worktree, including failures or skipped checks.
- Any public interface changes: env vars, `radio.toml` keys, HA add-on options,
  API routes/payloads, status fields, or admin/listener UI controls.
- A note on sibling paths checked for the same failure mode.

Reject or park slices that are not materially part of Festival Mode.

## Integration Rules

- Prefer small integration commits with conventional prefixes only:
  `feat:`, `fix:`, `chore:`, `ci:`, `deps:`.
- Preserve existing product contracts unless the festival feature explicitly changes
  them. Existing Chaos Mode and Super Italian Mode patterns are the closest models
  for live mode toggles and persisted station personality switches.
- Keep `.context/` untouched. It is runtime and collaboration state, not source.
- If lifecycle behavior changes, update `conductor.json` and the related
  `scripts/conductor-*.sh` files in the same change.
- If a version bump happens, keep `CHANGELOG.md` and
  `ha-addon/mammamiradio/CHANGELOG.md` synchronized with the shipped feature.
- Keep all fictional ad brands fictional. Festival content must not introduce real
  companies, products, trademarks, or named public figures into generated ad copy.

## Merge Gate

Before this train can merge to `main`:

- All accepted Festival Mode slices read as one coherent feature.
- Public behavior has focused regression coverage.
- Sibling code paths for the same invariant have been checked.
- HA add-on config, translations, `rootfs/run.sh`, docs, and env handling are in sync
  if the feature adds a persisted option.
- Admin/listener UI changes preserve the existing interaction model and design-system
  constraints unless an intentional festival-specific change is documented.
- Root and add-on changelogs describe the feature if there is a version bump.
- The repo validation passes locally with `make check` or the closest available
  equivalent if `make check` is unavailable.

## Suggested Review Order

1. Confirm the slice is in scope for Festival Mode.
2. Read public interface changes first, then implementation, then tests.
3. Check config/env/add-on synchronization before UI polish.
4. Run the smallest relevant tests for the touched subsystem.
5. Run the full merge gate once all accepted slices are integrated.

## Handoff Template

Use this shape when another worktree hands work to the train:

```text
Slice:
Subsystem:
Changed files:
Behavior:
Public interfaces:
Validation:
Sibling paths checked:
Risks:
```
