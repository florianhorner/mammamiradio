# Listening Slice B: validation

Path A to `main`. Workspace `tegucigalpa`. Branch `florianhorner/feat/listening-slice-b`.
Original review merge base: `7829fb728611cd99e9fc79d39f1a3164a7e36142`; original implementation commit: `b0b00c27fb8673d676525abdcc397213472e64dd`.
`/ship` merged `origin/main` on top (clean 3-way merge, no conflicts) and ran a fresh
pre-ship review pass against the result. Current merge base:
`9b2b72ab167f53cb3d12dc6a3517a669eddf86c6`; final HEAD: `1bbeee344b276ab9b1b4c4933a2e2e33aa6ec709`.

## Write-set

Assigned product/test paths:
- `mammamiradio/home/ha_context.py`
- `mammamiradio/scheduling/producer.py`
- `mammamiradio/web/streamer.py`
- `mammamiradio/web/templates/admin.html`
- `docs/operations.md`
- `tests/home/test_ha_context.py`
- `tests/home/test_ha_media_player_push_flag.py`
- `tests/scheduling/test_producer.py`
- `tests/web/test_streamer_routes_extended.py`
- `tests/web/test_public_status_contract.py`
- `tests/web/test_admin_status_invariants.py`
Allowed evidence: `proof/listening-slice-b-validation.md` and `proof/preship-reviews/v2/**`.
No extra XSS file remains in the final diff. No integrator-only files changed.

## Review remediation (original implementation)

- Ghost DELETE HTTP/transport/unexpected failures now join canonical failure aggregation, including when sensors are deduped.
- Streamer status tests reset HA push globals; producer tests invoke and assert the real heartbeat result callback.
- The playback-selection immediate push has a regression test.
- Admin next steps select standalone versus add-on guidance from `config.is_addon`.
- Operations docs describe 30-second first wait, then 60/120/240/300-second failure backoff.

## Review remediation (`/ship` pre-landing pass)

Nine subagents ran against the merged diff: coverage audit, plan-completion
audit, five specialists (testing, maintainability, security, performance,
api-contract, design), and Claude adversarial, plus Codex adversarial and
Codex structured review (`--base main`, P1 gate). Fixed:

- 4 coverage gaps closed with mutation-verified tests: the `attempted==0 → None`
  no-op contract, worst-reason-wins across mixed failure categories in one
  cycle, the addon `auth_denied` copy override, and the non-recovered "ok"
  wording.
- `recovered`/`ok`/`idle` status copy now routes through `_ha_publish_copy()`
  like every other branch, so a future addon-specific override can't be
  silently skipped by three call sites that read the copy dict directly.
- The three generic-exception branches now log the exception class name only
  (never message/repr) at DEBUG, so an unrelated real bug doesn't sit
  invisible forever behind the sanitized "unexpected" reason.
- Fixed indentation of the pre-existing `ha_details` block in
  `updateEngineRoom()` (whitespace only, brace nesting was ambiguous under the
  new `pub||hd` split).
- Consolidated the `_ha_publish_health` test reset into the existing autouse
  fixture instead of duplicating it inline in two tests (maintainability +
  testing specialists both flagged this independently).
- Documented `runtime_health.ha_publish`'s shape in `docs/operations.md`.
- Codex structured review (P1 gate) found 2 real P2s. `ha_publish_status_payload`
  reported `enabled: false` for the `unconfigured` status even when the
  operator had turned HA on in config, making it indistinguishable from
  `disabled` to anything reading the `enabled` field; it now reports the
  actual config toggle. The just-added `reason` doc line claimed empty outside
  `degraded`, but the `ok`/`recovered` path has always set `reason: "ok"`.
  Fixed the table, not the already-reviewed code.
- The Codex P1 (evidence staleness) is resolved by this revision of this file
  and a fresh `proof/preship-reviews/v2/` receipt at final HEAD.

One design finding was investigated and found to be intended behavior, not a
bug. The HA card now always renders (even fully-disabled installs show
"Publishing: off") because `ha_publish_status_payload()` never returns null.
That is exactly what the plan asked for ("render publishing status even when
`ha_details` is absent"), verified against
`.context/plans/listening-slice-b-home-assistant-push-recovery.md`.

Deferred, not blocking (out of scope for this slice, noted for the integrator):
admin.html's `homePublishPresentation()`/`updateEngineRoom()` JS has no
behavioral test coverage. This repo has no JS runtime/test harness at all, a
pre-existing structural gap, not something introduced here. Backoff is global
per push-cycle, not per-entity, which is a strict improvement over the
pre-diff code (whose backoff never fired for real HTTP/connectivity
failures); narrowing further is a follow-up enhancement. Unbounded
`ha_push_tasks`/lock-waiter growth if HA is reachable but non-responsive for
a sustained period is a pre-existing pattern this diff doesn't worsen
(confirmed it never touches the FFmpeg/audio path either way).

## Validation

- Focused purge/XSS regression tests: `29 passed`.
- Full `make test` (fresh run at final HEAD, post-Codex-fixes): `8821 passed,
  4 skipped, 49 deselected, 1 warning` in 763s; 92.41% coverage (ratchet floor
  92.0%). `make check`'s lint/format/typecheck/deadcode/media-check layers
  independently reproduced clean during the `/ship` pass.
- Real Admin browser guard: `1 passed in 8.12s` via `ADMIN_BROWSER_SMOKE_URL=http://127.0.0.1:8000`.
- Manual local `/admin`: HTTP 200, Home Assistant publishing panel rendered disabled state, no console errors, same-origin API/static requests succeeded. Existing fixture evidence: `.context/plans/ha-publish-admin-qa.html` and its three PNGs.
- Final merge-base diff: 13 files, 1044 insertions, 166 deletions (1210 changed lines).

## Boundaries

No live Home Assistant or physical/audible player validation was performed. The implementation is locally verified; live HA credentials, deployment, and release metadata remain integrator-owned follow-up.

## Integrator note

Under the current Unreleased heading in `CHANGELOG.md` and `ha-addon/mammamiradio/CHANGELOG.md`, add a Fixed bullet that failed Home Assistant entity updates back off the heartbeat and recover without leaking HA bodies or tokens in logs or `/status`.

Immutable receipt: the final content-addressed receipt is committed under `proof/preship-reviews/v2/`.
