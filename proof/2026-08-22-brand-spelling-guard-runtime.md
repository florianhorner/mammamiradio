# Brand spelling guard runtime proof

Tested code SHA: `15f2d9d7c32a6c6169f958f6a0345c651daad537`

The final pass ran on an isolated loopback server with Anthropic, OpenAI, Azure,
ElevenLabs, Jamendo, and Home Assistant credentials explicitly cleared, yt-dlp
disabled, and temporary state under `tmp/qa-pr-1011-final/`. Neither `.context/`,
Home Assistant, nor a deployed add-on was touched.

A preliminary manual playback run inherited a local ElevenLabs key from `.env`
and made one TTS request while filling the isolated local queue. That run was
discarded. Both browser guards below were repeated against the credential-free
final runtime.

## Admin QA: PASS

- The full executable Admin browser guard passed: 1 test in 6.58 seconds.
- The real `/admin` page rendered the `media_source_missing` state with the
  corrected `Mamma Mi Radio` recovery text.
- The error state remained readable at desktop and 375 x 812.
- Protected controls, keyboard behavior, 320–768 px layouts, retry states, and
  uncaught page errors passed in the browser guard.

## Player QA: PASS

- The separate executable Player smoke returned `ok: true`.
- Stream intent reached the audio element in 22 ms.
- Station identity remained `Mamma Mi Radio`.
- The live page played `/stream`, showed now-playing and three up-next rows, and
  changed all play controls to `Pause station`.
- The 375 x 812 layout had zero horizontal overflow and no console errors.

## Runtime health: PASS

- `/healthz` returned `status: ok` with no listener silence.
- `/readyz` returned `status: ready`; producer and playback tasks were alive.
- `pytest tests/web/test_route_smoke.py -q`: 18 passed.

The screenshots and detailed report are under
`.gstack/qa-reports/qa-report-127-0-0-1-2026-08-22.md`. This receipt records
local branch behavior only; it does not claim deployment or Home Assistant QA.
