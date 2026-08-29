import assert from "node:assert/strict";
import { access, readFile } from "node:fs/promises";
import { execFile } from "node:child_process";
import { promisify } from "node:util";
import test from "node:test";

const run = promisify(execFile);
await run(process.execPath, ["scripts/build.mjs"]);
const html = await readFile("dist/index.html", "utf8");
const escapeRegExp = (value) => value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

test("every scenario ships a real host clip, and none is browser speech", async () => {
  // The whole point of the audio is that a visitor hears Marco and Giulia
  // rather than a speech engine. A scenario whose clip did not make it into the
  // build would leave a play button pointing at a 404, which teaches exactly
  // the wrong thing about the product.
  const ids = [...new Set([...html.matchAll(/data-scenario-id="([a-z]+)"/g)].map((m) => m[1]))];
  assert.ok(ids.length >= 4, "expected the four home moments");
  for (const id of ids) {
    await access(`dist/public/audio/${id}.mp3`);
  }
  const appJs = await readFile("dist/app.js", "utf8");
  assert.doesNotMatch(appJs, /speechSynthesis/, "browser TTS must not come back");
  assert.match(appJs, /public\/audio\//);
  // The browser module graph dies silently if a local import is missing from
  // dist/. phase.mjs shipped in source and not in the build once; never again.
  const localImports = [...appJs.matchAll(/(?:^|\n)\s*import\s[^"']*["'](\.\/[^"']+)["']/g)].map(
    (match) => match[1].replace(/^\.\//, ""),
  );
  assert.ok(localImports.includes("phase.mjs"), "app.js must import phase.mjs");
  for (const moduleName of new Set(localImports)) {
    await access(`dist/${moduleName}`);
  }
});

test("the built root page contains both outbound exits in order", async () => {
  await access("dist/.nojekyll");
  const addonUrl = "https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Fflorianhorner%2Fmammamiradio";
  const sourceUrl = "https://github.com/florianhorner/mammamiradio";
  assert.match(html, new RegExp(`href="${escapeRegExp(addonUrl)}"`));
  assert.match(html, new RegExp(`href="${escapeRegExp(sourceUrl)}"`));
  assert.ok(html.indexOf(addonUrl) < html.indexOf(sourceUrl));
  assert.match(html, /class="footer-links"/);
  assert.match(html, /\/\/ listen:/);
  assert.doesNotMatch(html, /<a[^>]*>Listen live<\/a>/i);
  assert.match(html, /href="shorts\/"[^>]*>Watch Studio B Transmissions<\/a>/);
});

test("the Studio B hub and exactly three watch routes ship", async () => {
  const episodes = [
    {
      slug: "archive-receipt",
      title: "Archive Receipt",
      asset: "mamma-mi-radio-studio-b-archive-receipt.mp4",
      poster: "archive-receipt.png",
    },
    {
      slug: "jealous-microphone",
      title: "Jealous Microphone",
      asset: "mamma-mi-radio-studio-b-jealous-microphone.mp4",
      poster: "jealous-microphone.png",
    },
    {
      slug: "third-chair",
      title: "Third Chair",
      asset: "mamma-mi-radio-studio-b-third-chair.mp4",
      poster: "third-chair.png",
    },
  ];
  const hub = await readFile("dist/shorts/index.html", "utf8");
  assert.equal([...hub.matchAll(/class="episode-card"/g)].length, episodes.length);
  assert.match(hub, /Contains synthetic voices\./);

  for (const episode of episodes) {
    assert.match(hub, new RegExp(`href="${episode.slug}/"`));
    const watchPath = `dist/shorts/${episode.slug}/index.html`;
    const watch = await readFile(watchPath, "utf8");
    const assetUrl = `https://github.com/florianhorner/mammamiradio/releases/download/v2.18.0/${episode.asset}`;
    assert.match(watch, new RegExp(`<link rel="canonical" href="https://florianhorner\\.github\\.io/mammamiradio/shorts/${episode.slug}/"`));
    assert.match(watch, /<video controls playsinline preload="metadata"/);
    assert.match(watch, new RegExp(`aria-label="[^\"]*${episode.title}[^\"]*"`));
    assert.doesNotMatch(watch, /<video[^>]*\sautoplay(?:\s|=|>)/i);
    assert.equal(watch.split(assetUrl).length - 1, 3, "source, fallback, and direct-download links must agree");
    assert.match(watch, /Contains synthetic voices\./);
    assert.match(watch, /href="\.\.\/\.\.\/"/);

    const poster = await readFile(`dist/shorts/posters/${episode.poster}`);
    assert.ok(poster.subarray(0, 8).equals(Buffer.from("89504e470d0a1a0a", "hex")));
    assert.equal(poster.readUInt32BE(16), 540);
    assert.equal(poster.readUInt32BE(20), 960);
  }
  for (const page of [hub, ...await Promise.all(episodes.map((episode) => readFile(`dist/shorts/${episode.slug}/index.html`, "utf8")))]) {
    assert.match(page, /<meta name="twitter:image:alt" content="Mamma Mi Radio:/);
    assert.match(page, /<meta property="og:image:width" content="1280"/);
    assert.match(page, /<meta property="og:image:height" content="640"/);
  }
  await access("dist/shorts/styles.css");
});

test("the Pages root keeps runtime assets subpath-safe", () => {
  assert.doesNotMatch(html, /(?:href|src)="\//);
  assert.match(html, /href="styles\.css"/);
  assert.match(html, /src="app\.js"/);
  assert.match(html, /href="public\/favicon\.svg"/);
});

test("link previews point at a card that actually ships, at its real size", async () => {
  // A relative og:image previews as a bare link: scrapers fetch the tag value
  // as-is and do not resolve it against the page. So these four are the only
  // absolute URLs on an otherwise subpath-safe page, and drift between them
  // is the failure nobody notices until a link is already posted.
  const origin = "https://florianhorner.github.io/mammamiradio/";
  const value = (pattern) => {
    const match = html.match(pattern);
    assert.ok(match, `missing link-preview tag: ${pattern}`);
    return match[1];
  };
  const canonical = value(/<link rel="canonical" href="([^"]+)"/);
  const ogUrl = value(/<meta property="og:url" content="([^"]+)"/);
  const ogImage = value(/<meta property="og:image" content="([^"]+)"/);
  const twitterImage = value(/<meta name="twitter:image" content="([^"]+)"/);
  assert.equal(canonical, origin);
  assert.equal(ogUrl, origin);
  assert.equal(ogImage, twitterImage, "og:image and twitter:image must agree");
  for (const url of [ogImage, twitterImage]) {
    assert.ok(url.startsWith(origin), `${url} must be absolute and on ${origin}`);
  }

  // The card has to be in the build, not merely in source — dist/ is what
  // Pages serves, and build.mjs's copy list is hand-maintained.
  const imagePath = ogImage.slice(origin.length);
  const bytes = await readFile(`dist/${imagePath}`);
  // The full 8-byte signature, not just the "PNG" in the middle of it: byte 0
  // is 0x89 and the trailing CR LF SUB LF exist to catch a file mangled in
  // transit. Checking three bytes would pass a truncated or wrong binary that
  // happens to spell PNG, which is the opposite of what this line claims.
  assert.ok(
    bytes.subarray(0, 8).equals(Buffer.from("89504e470d0a1a0a", "hex")),
    "the share card must carry a real PNG signature",
  );
  assert.ok(bytes.length >= 24, "a PNG shorter than its own IHDR header cannot be read for dimensions");

  // Twitter/X and Slack size the card from these; a re-rendered card of a
  // different size would otherwise preview cropped.
  assert.equal(bytes.subarray(12, 16).toString("ascii"), "IHDR", "PNG must open with IHDR before its dimensions are read");
  const width = bytes.readUInt32BE(16);
  const height = bytes.readUInt32BE(20);
  assert.equal(value(/<meta property="og:image:width" content="([^"]+)"/), String(width));
  assert.equal(value(/<meta property="og:image:height" content="([^"]+)"/), String(height));
  assert.equal(value(/<meta property="og:image:type" content="([^"]+)"/), "image/png", "the declared type must match the bytes read above");
  assert.match(html, /<meta name="twitter:card" content="summary_large_image"/);
  assert.match(html, /<meta property="og:type" content="website"/);
  assert.match(html, /<meta property="og:site_name" content="Mamma Mi Radio"/);
  assert.match(html, /<meta property="og:locale" content="/);
});

test("the twitter card cannot drift away from the og card", () => {
  // Four strings are written twice, once for og: and once for twitter:. The
  // URL pair is guarded above; these three are prose, which is the half that
  // actually gets copy-edited on one line and forgotten on the other. Drift
  // here is invisible to any single person: X reads one string, Slack and
  // iMessage read the other, so nobody ever sees both at once.
  const pairs = [
    ["og:title", "twitter:title"],
    ["og:description", "twitter:description"],
    ["og:image:alt", "twitter:image:alt"],
  ];
  for (const [ogName, twitterName] of pairs) {
    const og = html.match(new RegExp(`<meta property="${ogName}" content="([^"]+)"`));
    const twitter = html.match(new RegExp(`<meta name="${twitterName}" content="([^"]+)"`));
    assert.ok(og, `missing ${ogName}`);
    assert.ok(twitter, `missing ${twitterName}`);
    assert.equal(twitter[1], og[1], `${twitterName} must say exactly what ${ogName} says`);
    assert.ok(og[1].length >= 20, `${ogName} is too short to be real copy`);
  }
});

test("the deployed origin is named only by the link-preview block", () => {
  // The HTML comment next to those tags makes this claim. Without a count it
  // is decoration: a later rel=alternate, a JSON-LD block, or one runtime
  // asset hard-pinned to the origin would pass every other test here and
  // 404 on a fork, a rename, or any other host.
  const origin = "https://florianhorner.github.io/mammamiradio/";
  const occurrences = html.split(origin).length - 1;
  assert.equal(occurrences, 4, "only canonical, og:url, og:image and twitter:image may name the origin");
  // rel=canonical is the one legitimate href on the origin. Anything else
  // with an href or src pinned there is a runtime asset that would 404 on
  // a fork, a rename, or any other host.
  const pinned = [...html.matchAll(/(?:href|src)="https:\/\/florianhorner\.github\.io[^"]*"/g)].map((m) => m[0]);
  assert.deepEqual(pinned, [`href="${origin}"`], "only rel=canonical may point an href at the origin");
});
