# Brand spelling guard runtime proof

Tested integrated code SHA: `e9eb6e7dc002bbf6225f4809fae6d21933d91fbd`

The final pass used an isolated loopback server at `http://127.0.0.1:8766`.
Anthropic, OpenAI, Azure, ElevenLabs, Jamendo, and Home Assistant credentials
were explicitly blank, yt-dlp was disabled, and temporary state stayed under
`tmp/qa-pr-1011/`. The run did not write to `.context/`, contact Home
Assistant, update an add-on, or exercise a deployed runtime.

The manual Player play action caused the app's credential-free Edge TTS
fallback to synthesize one local banter asset while filling the isolated queue.
That was external network activity; no key-backed provider was used.

## Admin QA: PASS

- The Admin executable real-browser guard passed separately: 2 tests in 7.43
  seconds.
- It rendered the real `/admin` page and covered the `media_source_missing`
  state with the corrected `Mamma Mi Radio` recovery text, protected controls,
  keyboard behavior, retry states, layouts from 320 to 768 px, and uncaught
  page errors.
- A separate manual pass rendered `/admin` at desktop and 375 x 812 with no
  horizontal overflow or initial console errors. The deliberate no-Home-
  Assistant retry returned its expected 409 and displayed the controlled
  recovery state.

## Player QA: PASS

- The deterministic Player browser smoke passed separately against `/`.
- A manual play action reached `/stream`; the audio element reported
  `paused=false`, all three controls changed to `Pause station`, and three
  schedule rows were present.
- Desktop and 375 x 812 checks had no console errors or horizontal overflow.

## Runtime health: PASS

- `/healthz` returned `status: ok`; queue and shadow queue were both at depth
  4 and in sync, with no listener silence.
- `/readyz` returned `status: ready`; producer and playback tasks were alive.
- `pytest tests/web/test_route_smoke.py -q`: 18 passed.

The ignored `.gstack/qa-reports/qa-report-127-0-0-1-2026-08-23.md` file holds
the screenshots and detailed local report. This receipt covers local integrated
branch behavior only.
