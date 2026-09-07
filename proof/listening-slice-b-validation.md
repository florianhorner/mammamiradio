# Listening Slice B — validation

Path A to `main`. Workspace `tegucigalpa`. Branch `florianhorner/feat/listening-slice-b`.
Review merge base: `7829fb728611cd99e9fc79d39f1a3164a7e36142`; final HEAD: `b0b00c27fb8673d676525abdcc397213472e64dd`.

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

## Review remediation

- Ghost DELETE HTTP/transport/unexpected failures now join canonical failure aggregation, including when sensors are deduped.
- Streamer status tests reset HA push globals; producer tests invoke and assert the real heartbeat result callback.
- The playback-selection immediate push has a regression test.
- Admin next steps select standalone versus add-on guidance from `config.is_addon`.
- Operations docs describe 30-second first wait, then 60/120/240/300-second failure backoff.

## Validation

- Focused purge/XSS regression tests: `29 passed`.
- `make check`: `8815 passed, 4 skipped, 49 deselected, 1 warning`; 92.56% coverage, 93% ratchet; media proof, Ruff, format, mypy, vulture, and coverage floors passed.
- Real Admin browser guard: `1 passed in 8.12s` via `ADMIN_BROWSER_SMOKE_URL=http://127.0.0.1:8000`.
- Manual local `/admin`: HTTP 200, Home Assistant publishing panel rendered disabled state, no console errors, same-origin API/static requests succeeded. Existing fixture evidence: `.context/plans/ha-publish-admin-qa.html` and its three PNGs.
- Final merge-base diff: 13 files, 867 insertions, 132 deletions (999 changed lines); no push, PR, or external publication.

## Boundaries

No live Home Assistant or physical/audible player validation was performed. The implementation is locally verified; live HA credentials, deployment, and release metadata remain integrator-owned follow-up.

## Integrator note

Under the current Unreleased heading in `CHANGELOG.md` and `ha-addon/mammamiradio/CHANGELOG.md`, add a Fixed bullet that failed Home Assistant entity updates back off the heartbeat and recover without leaking HA bodies or tokens in logs or `/status`.

Immutable receipt: `proof/preship-reviews/v2/129b9fe35da707a1e4e92d57e6978a0005e501a21035c446cdf10c87785ffcc9/5daf7addd9c9c99171bb0f01dcfaad662f8dbc95113c046a4c2109f4bb13784c.json`.
