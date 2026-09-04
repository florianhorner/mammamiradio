# Mamma Mi Radio interactive explainer

An interactive page that plays the station first. You tune in, a show is
already running, and somewhere inside it the hosts mention something only your
house could have told them. The sensor data arrives after the moment, as liner
notes.

The scenarios are invented. Marco and Giulia are not: the clips in
`public/audio/` are the station's own hosts in their own configured voices.
One of the four moments ("Evening, officially") uses only the sun and the
weather, which is all a fresh install can share. The other three need the home
grant, and the page says so. The page reads no Home Assistant data and sends
nothing anywhere.

## How it is put together

- `scenarios.mjs`: the single source of truth. Copy, sensor rows,
  reachability, the segment script (`beats[]`), and the `revealAtSec` cue
  point live here and only here. Transcripts are derived from the beat lines,
  so they cannot drift from the audio.
- `phase.mjs`: the page's phase decisions as pure functions, testable without
  a browser. The phases run `idle`, `onair`, `revealed`, with `data-audio`
  marking `loading` and `failed`. Audio position drives the reveal. A deadline
  timer catches playback that never starts or stalls without an error event.
- `app.js`: wiring. One play path (`playSegment`), a transport that shows
  playback position, and a distinct failed state. A visitor who heard nothing
  is never shown a successful on-air moment; the card shows the transcript
  instead and names the way forward.

## Local preview

```sh
npm install
npm run dev
```

Then open <http://127.0.0.1:4187/>. The preview binds to loopback only and
serves HTTP Range requests, so audio seeking behaves the same as on GitHub
Pages.

## Validate

```sh
npm test          # source and built-output checks, no browser needed
npm run build     # writes the static site to dist/
npm run test:e2e  # drives the funnel in Chromium; run `npx playwright install chromium` once
```

The build fails rather than shipping a broken page when a scenario in
`index.html` and `scenarios.mjs` disagree, a clip is missing, a derived
transcript is empty, no scenario is fresh-install reachable, or a produced
clip has a `revealAtSec` that is absent or falls outside the clip.

The unit tests pin the framing (station first, sensors as the reveal, no
pipeline ordinals, including the meta description), the failure contract, the
transport, the day-one scenario's ambient-only entities, and the responsive
and reduced-motion treatments, plus the link-preview card: that its URLs
agree, that the og: and twitter: copy cannot drift apart, that the card ships
in `dist/`, and that the declared size matches the real PNG. The end-to-end
test clicks through the real
page: tune in, reveal at the cue, install button, retirement after the fourth
moment. It runs under emulated reduced motion, which also exercises that
contract on every run.

## Producing the clips

Two scripts, both operator commands rather than CI steps:

- `scripts/render-voice-beats.py` renders the host lines from `scenarios.mjs`
  in the station's configured voices. Paid per character; it skips existing
  files unless you pass `--force`.
- `scripts/produce-segments.mjs` assembles each scenario's `beats[]` (a
  starter-catalog music tail, station imaging from
  `mammamiradio/assets/imaging/`, and the rendered speech) into one continuous
  segment, measures where the home moment lands, and writes
  `public/audio/segments.manifest.json` with per-clip checksums and the
  measured `revealAtSec`. Run it without flags for a dry run that names every
  missing input.

## Publishing

`.github/workflows/explainer-pages.yml` is the publish path. A push to `main`
touching `docs/explainer/**` or that workflow runs `npm test`, `npm run build`,
and deploys `dist/` to GitHub Pages at
<https://florianhorner.github.io/mammamiradio/>. The deploy job is gated on
`refs/heads/main`, so the dispatch button tests a branch without publishing it.
Operator detail lives in `docs/operations.md` under "Explainer page deployment".

The same Pages artifact carries the Studio B hub and three watch routes:

- <https://florianhorner.github.io/mammamiradio/shorts/>
- <https://florianhorner.github.io/mammamiradio/shorts/archive-receipt/>
- <https://florianhorner.github.io/mammamiradio/shorts/jealous-microphone/>
- <https://florianhorner.github.io/mammamiradio/shorts/third-chair/>

The approved MP4 masters remain outside Git and are loaded from tag-pinned
GitHub Release asset URLs. Every public shorts page states `Contains synthetic
voices.` Before publishing a changed master, verify playback and seeking in
Safari and Chromium, then update the pinned URL only after the replacement is
live. Do not add MP4 files under `docs/explainer/`.

The directory root is also a complete static site on its own: prebuilt files
plus `.nojekyll`, no build step needed to read it. Serving it from another
origin needs one edit first. `index.html` names
`https://florianhorner.github.io/mammamiradio/` in `rel="canonical"`, `og:url`,
`og:image` and `twitter:image`, and `tests/rendered-html.test.mjs` pins the
same string, so a copy served elsewhere would advertise a canonical URL and a
link-preview card pointing back here.
