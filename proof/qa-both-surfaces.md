# Player and Admin QA

This receipt covers code SHA `16c78caa1318848a8071d5ec09b8a815ca614d96`
against isolated loopback servers. External network access was denied for the
station processes. It is local proof, not Home Assistant deployment proof.

## Player

The executable Chromium smoke drove the real listener page and its interaction
controller. It passed 15 checks:

- stream intent reached the audio element in 22 ms;
- station identity remained `Mamma Mi Radio`;
- shout-out and song-request success paths rendered correctly;
- rate-limited, queue-full, declined, and network-failure request paths kept an
  actionable listener state;
- the page retained its audio element, listener controls, on-air copy, and
  accessible state;
- the only blocked off-origin request was the optional Google Fonts stylesheet;
- no unexpected page or console error occurred.

## Admin

The executable Chromium Admin guard passed against the real `/admin`, `/status`,
and `/api/setup/status` surfaces. Same-origin response interception was used only
to deterministically exercise failure and corrupt-install branches.

| Contract | Result |
|---|---|
| Healthy Start sends one normal `POST /api/resume` and no force request | PASS |
| Assetless refusal leaves the station paused and offers a way out | PASS |
| Cancelling the confirmation sends no force request | PASS |
| Confirming sends exactly one `POST /api/resume?force=true` | PASS |
| Failed/malformed/stale status polls retain last-good truth and an accessible retry | PASS |
| Setup remains source-readiness guidance while `/status` owns paused runtime truth | PASS |
| Metadata-only copy distinguishes paused commits from epoch-race commits | PASS |
| Stopped controls, keyboard focus, reduced motion, and 320–768 px layouts stay coherent | PASS |
| Unexpected page or console errors | None |

## Transport and race QA

The browser guard covers the controllers; deterministic ASGI regressions cover
the backend concurrency boundaries that cannot be timed reliably by a visual
click script:

- a second Skip is declined while the first Skip history write is in flight;
- Stop wins over an in-flight Skip without resurrecting transport state;
- Panic during Skip preserves exactly-once listener history;
- a source load crossing Stop commits filtered metadata only;
- a source load crossing a fast Resume preserves the entire queued runway;
- playlist enrichment and external-download completion crossing Stop/Resume
  update metadata without injecting stale audio.

All seven focused race tests passed.

## Status and restart QA

- `/healthz` stayed healthy throughout the launch scenarios.
- `/readyz` stayed `starting` until a listener accepted audio.
- `/readyz` and `/public-status` both reported an intentional persisted stop.
- `/status` and the Admin runtime card treated paused state as runtime truth,
  not a setup failure.
- `/api/setup/status` remained focused on source and credential readiness.
- Warm, empty-cache packaged, exact-image, and persisted-stop scenarios are
  recorded in `proof/stop-resume-continuity.txt`.

## Evidence format

The final receipt uses executable browser/API results rather than retaining
screenshots captured before the last runtime and Admin changes. Timing,
confirmation ownership, and concurrency are not provable from static pixels.

## Residual behavior outside this remediation

An already-open listener page does not automatically invoke browser playback
after an operator Resume; it returns to the on-air state and the listener may
need to press Listen Now. This predates the PR #914 remediation and was not
changed here.
