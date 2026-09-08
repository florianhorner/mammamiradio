# Listening Slice C validation

Runtime implementation: `a8bc9fc085fd91aa07aca475969021190a28dad1`. Base: `8be9a29932c2ac3ae7782ec3a7ff4c366f65e665`.
Player smoke fixture blob: `6540933c40351a9e7d7625fb4d6172b2b3a350db`.

| Check | Observed result |
|---|---|
| Initial transition, memory and label-budget regressions | 4 failed, 2 passed before implementation |
| Catalog persistence regressions | 2 failed before the persistence correction |
| Focused language, writer, memory, catalog and route suite | 907 passed |
| `COVERAGE_RATCHET_XDIST=4 make check` | Exit 0; 8,997 passed, 4 skipped; 93% coverage against a 92% floor, all per-module floors held; lint, formatting, types, dead-code and media checks passed |
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

Label generation permits at most two application-level requests per attempt, with SDK retries disabled and a per-attempt deadline scaled to the requested tokens (45 seconds at 1,200, 120 at 4,000), so the catalog lock is bounded by at most two attempts and a slow first attempt cannot discard the retry's own labels. Tests cover half-batch success, repeated truncation, later-poll progress, old-data preservation, private-log canaries, and revocation during a cancellation-resistant truncated request: one call, no retry, no save. The temp file is created owner-only before any label reaches the disk, and no fallible step follows the atomic replacement.

Counters distinguish first language rejection from terminal/final-text failure, with all writer fallback paths covered. Other language floors and public status remain unchanged. English words in titles/artists still count as markers; repeated random fallback selection remains possible.

Independent source reviews covered correctness/sibling paths, tests, privacy and docs/config consistency. Persistence fault coverage and terminal counter assertions were added in response. Review-bot healing after that pass corrected three further items on the same surfaces: the label catalog temp file is now created owner-only instead of being chmod-ed after the household labels were written; SDK-level retries are off and one wall-clock budget covers both truncation attempts, so a stalled provider cannot hold the catalog lock for several timeouts; and the three added stock exchanges were rebuilt on Studio B lore. Each carries a regression test verified to fail against the pre-fix implementation. Human voice/mix audition is complete. On 2026-09-08 all four Normal Mode stock exchanges and both representative transitions were rendered through `synthesize_dialogue` on the configured production ElevenLabs host voices, played back on the maintainer's own speakers, and accepted individually: `fallback_0` through `fallback_3`, `transition_english`, `transition_italian`, six of six kept. The unchanged approved exchange and the unchanged English handoff were rendered alongside the new copy as listening references. The Italian handoff under audition was checked against both floors in the same run: the ordinary Normal Mode floor rejects it and the transition floor admits it, so the clip demonstrates the behavior change rather than restating it. No deployment or live-provider recovery is claimed.
