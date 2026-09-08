# Listening Slice C validation

Runtime implementation: `afda5507f129e2e56fa4dd59eda04c7f59754248`. Base: `8f69ac045cb57ecd434191520ae9071fa5d96852`.
Player smoke fixture blob: `6540933c40351a9e7d7625fb4d6172b2b3a350db`.

| Check | Observed result |
|---|---|
| Initial transition, memory and label-budget regressions | 4 failed, 2 passed before implementation |
| Catalog persistence regressions | 2 failed before the persistence correction |
| Focused language, writer, memory, catalog and route suite | 907 passed |
| `COVERAGE_RATCHET_XDIST=4 make check` | Exit 0; 8,917 passed, 4 skipped; 93% coverage; lint, formatting, types, dead-code and media checks passed |
| Player browser harness | Exit 0; stream intent 44 ms; stopped recovery 3,492 ms; 16 request scenarios |
| Configured Admin browser test | Exit 0; 2 passed |
| Browser contract tests after fixture correction | 7 passed, 1 opt-in test skipped; Admin executed separately above |

The full check ran on the runtime implementation above. Later changes are the six-line Player test-fixture reset and this report. The standard Player harness passed with that reset; earlier runs failed when Chrome reused the previous finite MP3 during the error scenario. No playback assertion was removed. The Admin server fixture was corrected from an existing install to a fresh install before its successful run.

Browser scope: local production routes, templates and JavaScript with synthetic state; no producer, Home Assistant connection or live credentials. Player desktop/mobile navigation and Admin runtime/provider rendering were inspected with no console errors. Admin displayed normally with `script_guard` values 4/2 alongside existing voice and HA diagnostics. Browser stream timing measures fixture behavior, not real speaker output.

| Output cap | Before | After |
|---|---:|---:|
| Memory extraction | 500 | 900 |
| Ad generation | 800 | 1,100 |
| Labels: 1 / 10 / 25 / 50 entities | 1,200 each | 1,200 / 1,200 / 2,200 / 4,000 |

Label generation permits at most two application-level requests per attempt, with unchanged SDK retries and a 45-second per-call timeout. Tests cover half-batch success, repeated truncation, later-poll progress, old-data preservation, private-log canaries, and revocation during a cancellation-resistant truncated request: one call, no retry, no save. Permissions are set before atomic replacement; no fallible step follows it.

Counters distinguish first language rejection from terminal/final-text failure, with all writer fallback paths covered. Other language floors and public status remain unchanged. English words in titles/artists still count as markers; repeated random fallback selection remains possible.

Independent source reviews covered correctness/sibling paths, tests, privacy and docs/config consistency. Persistence fault coverage and four terminal counter assertions were added in response. Human voice/mix audition remains pending: representative transition scripts and all four exchanges are prepared, but no audio was rendered or heard. No deployment or live-provider recovery is claimed.
