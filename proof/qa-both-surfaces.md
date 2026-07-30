# QA: Player and Admin surfaces

Local loopback station (`scripts/conductor-run.sh`, port 8123), real FFmpeg 8.1.1,
real Apple Music IT chart source (79 tracks), real normalization cache. Driven
through the actual UI with Playwright — buttons clicked, not endpoints poked,
except where a payload assertion is named below.

## Admin QA (`/admin`)

| Check | Result |
|---|---|
| Stop acknowledges from real JSON, no optimistic success | PASS — `#stopBtn` click, `data-stopped` flips to `true`, `session_stopped: true` |
| Paused with a configured source never reads "Needs setup" | PASS — zero "Needs setup" strings on the page while paused |
| Setup stays ready while runtime is paused | PASS — Setup reads "Ready"; runtime reads "Station paused · press Start when you're ready." |
| Stream Engine card uses runtime truth, not a hard-coded chip | PASS — `Stream Engine · Paused` when paused, `· Ready with backup` when running, `· On air with backup` on air |
| Provider rows: "Current" on air, "Last observed" when paused | PASS — `Current: Anthropic · …` on air; `Last observed: Anthropic · …` when paused |
| Primary label never welded to a stale fallback reason | PASS — switch history renders as its own timestamped line |
| Failover line is operator copy, not provider keys | PASS — `Audio source: Charts → Norm cache rescue · Fallback audio is currently on air · 10:57:49` |
| Failed status poll marks data stale | PASS — `Status may be out of date — still trying. Last updated 10:58:32` |
| Recovered poll clears the marker | PASS — freshness banner returns to hidden |
| Resume returns the station to running | PASS — `data-stopped: false`, `session_stopped: false` |
| Console errors | None. The three recorded errors are the injected `Error: offline` used to force the stale-poll state. |

Screenshots: `admin-qa-paused-setup-ready.png`, `admin-qa-paused-last-observed.png`.

## Player QA (`/`)

| Check | Result |
|---|---|
| Listener page serves and renders | PASS — HTTP 200, `<audio>` element present |
| Gold "Mi" accent | PASS — `span.mi` present |
| Italian tricolor band | PASS — `.tricolor-band` present |
| In-character copy | PASS — "In Onda", "On Air · 96,7 FM" |
| No machine words, no `undefined`/`null`/`NaN` on screen | PASS — zero matches |
| `/public-status.current_source` semantics unchanged | PASS — still the configured playlist source ("Current Italian charts", 79 tracks) |
| Console errors | None |

Screenshot: `player-qa-listener-on-air.png`.

## The regression this change exists to fix

Timings in `stop-resume-continuity.txt`. Headline: a station that booted with a
stopped marker left over from a prior process (Scenario 3) resumed and delivered
listener bytes in **0.001s**; a live Stop→Resume cycle delivered in **0.124s**.
Budget is 2.000s.
