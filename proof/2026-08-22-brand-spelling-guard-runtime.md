# Brand spelling guard runtime proof

Tested code SHA: `15f2d9d7c32a6c6169f958f6a0345c651daad537`

The final pass used an isolated loopback server. Before starting it, I cleared
Anthropic, OpenAI, Azure, ElevenLabs, Jamendo, and Home Assistant credentials. I
also disabled yt-dlp and kept temporary state under `tmp/qa-pr-1011-final/`.
The run did not write to `.context/`, connect to Home Assistant, or update an
add-on.

A preliminary playback check loaded a local ElevenLabs key from `.env` and sent
one TTS request while filling the isolated queue. Its live-browser observations
are labeled below; they do not prove provider isolation. I repeated both
executable browser guards with all provider credentials blank.

## Admin QA: PASS

- The Admin browser guard passed: 1 test in 6.58 seconds.
- The real `/admin` page rendered the `media_source_missing` state with the
  corrected `Mamma Mi Radio` recovery text.
- The error state remained readable at desktop and 375 x 812.
- The guard covered protected controls, keyboard behavior, 320-768 px layouts,
  retry states, and uncaught page errors.

## Player QA: PASS

- The separate executable Player smoke returned `ok: true`.
- Stream intent reached the audio element in 22 ms.
- Station identity remained `Mamma Mi Radio`.

The preliminary live-browser check also confirmed that `/stream` played,
now-playing and three up-next rows appeared, and each play control changed to
`Pause station`. At 375 x 812, the page had no horizontal overflow or console
errors.

## Runtime health: PASS

- `/healthz` returned `status: ok` with no listener silence.
- `/readyz` returned `status: ready`; producer and playback tasks were alive.
- `pytest tests/web/test_route_smoke.py -q`: 18 passed.

The ignored `.gstack/qa-reports/qa-report-127-0-0-1-2026-08-22.md` file holds
the screenshots and detailed local report. This receipt covers local branch
behavior only; it does not claim deployment or Home Assistant QA.
