# Mamma Mi Radio interactive explainer

An interactive page that turns raw Home Assistant sensor values into a
plain-English home moment, then plays how the station's hosts bring that moment
on air.

The scenarios are invented. The voices are not: the four clips in
`public/audio/` are Marco and Giulia, rendered in the station's own configured
voices. The page connects to nothing, reads no Home Assistant data, and sends
nothing anywhere.

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
footer links from the `window.mammamiSiteLinks` block in `index.html` and copies
one audio clip per scenario. The scenario ids are read from `index.html`, so a
new home moment without a matching `public/audio/<id>.mp3` fails the build
rather than shipping a play button that 404s.

The tests cover the built output, not just the source. They assert that every
scenario ships its clip, that the page never falls back to browser speech
synthesis, that both outbound links render in order, that the copy stays direct,
and that the responsive and reduced-motion treatments survive.

## Publishing

The directory root is a complete static site: prebuilt files plus `.nojekyll`,
no build step needed to read it. Point GitHub Pages at it, or serve `dist/` from
anywhere that serves files.
