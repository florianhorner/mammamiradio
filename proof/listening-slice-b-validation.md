# Listening Slice B — validation

Path A to `main`. Workspace `tegucigalpa`. Branch `florianhorner/feat/listening-slice-b`.
Assigned base `7829fb728611cd99e9fc79d39f1a3164a7e36142`.

## Write-set

Assigned (product + tests):
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

Extra (within ~30%): `tests/web/test_xss_regression.py`.
Allowed extras: this file and `proof/preship-reviews/v2/**`.

No integrator-only files. Changelog remains an integrator note.

## Commands

Focused HA / producer / public-contract / Admin invariant tests: 432 passed.
`make check`: 8816 passed, 4 skipped, 49 deselected. Coverage 92.56% (floor 92%). All module floors held.

Isolated Admin QA: fixture-injected states in `.context/plans/ha-publish-admin-qa.html` (gitignored). Screenshots:
- `.context/plans/ha-publish-failure.png` — failing/retrying, XSS payload stays escaped text
- `.context/plans/ha-publish-recovery.png` — recovered after a successful attempt
- `.context/plans/ha-publish-admin-qa-all.png` — disabled, unconfigured, not-yet-tested, failure, recovery

Player QA: not applicable. Listener audio paths and public contracts are unchanged; `ha_publish` is admin-only.

Live Home Assistant: not used.

## Reviews (read-only)

Correctness / sibling-path: heartbeat sleeps from real `push_state_to_ha` results (`True` reset 30s, `None` keep interval, `False` 30→60→120→240→300). Playback-selection and stop-transition still push immediately. HACS exclusion, sequential writes, retry limits, auxiliary dedup, and cancellation propagation are unchanged. `/status` copies `runtime_health` then adds `ha_publish`; `_runtime_health_snapshot`, `/public-status`, `/healthz`, and `/readyz` stay on the snapshot.

Privacy: one aggregate outage warning and one recovery INFO. No HTTP bodies, exception text, tokens, or URLs in push or ghost-purge logs. Authenticated `ha_publish` omits URL and token. Admin copy uses `esc()`.

Copy: disabled / unconfigured / not tested yet / retrying / recovered / working, with next steps. `docs/operations.md` documents bounded backoff without claiming every immediate notification follows the timer.

## Integrator note

Unreleased changelog (do not edit here): under the current Unreleased heading in `CHANGELOG.md` and `ha-addon/mammamiradio/CHANGELOG.md`, add a Fixed bullet that failed Home Assistant entity updates back off the heartbeat and recover without leaking HA bodies or tokens in logs or `/status`.
