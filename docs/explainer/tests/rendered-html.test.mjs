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
  assert.equal(bytes.subarray(1, 4).toString("ascii"), "PNG", "the share card must be a real PNG");

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
