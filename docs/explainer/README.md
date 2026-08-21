# Mamma Mi Radio interactive explainer

An interactive page that plays the station first: you tune in, a show is
already running, and somewhere inside it the hosts mention something only your
house could have told them. The sensors arrive afterward, as the reveal — the
liner notes, not the show.

The scenarios are invented. Marco and Giulia are not: the clips in
`public/audio/` are the station's own hosts — characters with history, in
their own configured voices. One of
the four moments ("Evening, officially") uses only the sun and the weather,
which is exactly what a fresh install can share — the other three need the
home grant, and the page says so. The page connects to nothing, reads no Home
Assistant data, and sends nothing anywhere.

## How it is put together

- `scenarios.mjs` — the single source of truth. Copy, sensor rows,
  reachability, the segment script (`beats[]`), the transcript, and the
  `revealAtSec` cue point all live here and only here.
- `phase.mjs` — the page's phase decisions as a pure function
  (`idle → onair → revealed`, with `data-audio` as `"" | loading | failed`),
  testable without a browser. Audio position drives the reveal; a deadline
  timer catches only the silent case where playback never begins.
- `app.js` — wiring. One play path (`playSegment`), a transport that shows
  playback position, and a distinct failed state: a visitor who heard nothing
  is never shown a successful on-air moment — the card says what they should
  have heard, as text, and names the way forward.

## Local preview

```sh
npm install
npm run dev
```

Then open <http://127.0.0.1:4187/>. The preview binds to loopback only.

## Validate

```sh
npm test
npm run build
```

`npm run build` writes a self-contained static site to `dist/`: it inlines the
footer links from the `window.mammamiSiteLinks` block in `index.html` and
copies one audio clip per scenario. The build fails rather than shipping a
broken page when: a scenario in `index.html` and `scenarios.mjs` disagree in
either direction, a clip is missing, a transcript is missing, no scenario is
fresh-install reachable, or a produced clip exists whose `revealAtSec` is
absent or outside the clip.

The tests cover the built output, not just the source. They pin the framing
(station first, sensors as the reveal, no pipeline ordinals — including the
meta description), the aired-truth failure contract, the transport, the
day-one scenario's ambient-only entities, and the responsive and
reduced-motion treatments.

## Producing the clips

`scripts/produce-segments.mjs` (see its header) composes each scenario's
`beats[]` — a starter-catalog music tail, station imaging from
`mammamiradio/assets/imaging/`, and rendered host speech — into one continuous
~20s segment per scenario, measures where the home moment lands, and writes
`public/audio/segments.manifest.json` with per-clip sha256 and the measured
`revealAtSec`. Until that manifest exists, the page reveals when the clip
ends, which suits the short pre-producer clips.

## Publishing

The directory root is a complete static site: prebuilt files plus `.nojekyll`,
no build step needed to read it. Point GitHub Pages at it, or serve `dist/`
from anywhere that serves files.
