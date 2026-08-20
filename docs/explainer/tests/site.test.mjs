import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const [html, css, js] = await Promise.all([
  readFile("index.html", "utf8"),
  readFile("styles.css", "utf8"),
  readFile("app.js", "utf8"),
]);
const addonUrl = "https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Fflorianhorner%2Fmammamiradio";
const sourceUrl = "https://github.com/florianhorner/mammamiradio";
const escapeRegExp = (value) => value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

test("the signature promise is in the first viewport", () => {
  assert.match(html, /radio station/i);
  assert.match(html, /listens to the house/i);
  assert.match(html, /Hear a moment/);
  assert.match(html, /What Home Assistant sees/);
  assert.match(html, /What a person understands/);
});

test("the page leads with the station, not with a pipeline", () => {
  // The product is a radio station that sometimes notices the house, not a
  // sensor-to-speech translator with radio styling. The page used to say the
  // second thing, which sells the copyable half and spends the surprise the
  // real moment depends on. These assertions guard the framing, not the
  // wording, so the copy can keep improving without the framing sliding back.
  const h1 = html.match(/<h1>([\s\S]*?)<\/h1>/)[1].replace(/<[^>]+>/g, " ");
  assert.match(h1, /radio station/i, "the headline names the station");
  assert.doesNotMatch(h1, /sensor|data|smart home/i, "the headline is not about sensors");

  // Ordinal steps read as a pipeline whatever their labels say.
  assert.doesNotMatch(html, /class="flow-number"/, "the flow steps carry no ordinals");

  // The old thesis sentence is the most translator-ish line the page ever had.
  assert.doesNotMatch(html, /Sensor data, finally understandable/);
});

test("the experience has multiple interactive home moments", () => {
  for (const id of ["arrival", "coffee", "laundry", "quiet"]) {
    assert.match(html, new RegExp(`data-scenario-id="${id}"`));
    assert.match(js, new RegExp(`${id}:`));
  }
});

test("the local concept makes its privacy boundary explicit", () => {
  assert.match(html, /This demo reads no Home Assistant data/);
  assert.match(html, /No live data connected/);
});

test("the page offers the install and source exits without a dead live link", () => {
  assert.match(html, /window\.mammamiSiteLinks\s*=\s*{/);
  assert.match(html, new RegExp(escapeRegExp(addonUrl)));
  assert.match(html, new RegExp(escapeRegExp(sourceUrl)));
  assert.ok(html.indexOf(addonUrl) < html.indexOf(sourceUrl));
  assert.match(html, /\/\/ listen:/);
  assert.doesNotMatch(html, /<a[^>]*>Listen live<\/a>/i);
  assert.match(js, /renderSiteLinks/);
});

test("the explanatory copy stays direct", () => {
  assert.doesNotMatch(html, /—|Accurate\. Useful\. Completely joyless|One home\. Two languages/);
  // The hero says what the thing is before it says how it works.
  assert.match(html, /class="hero-intro">[^<]*hosts/i);
});

test("the voice plays on the first click, not the second", () => {
  // The voice is the only part of this page that is not copyable, and it used
  // to sit behind a second click and a 2.9s animation, so a visitor could read
  // the whole page and never hear it. playFromGesture runs inside the button's
  // own handler, which is what makes autoplay permissible.
  assert.match(js, /function playFromGesture/);
  assert.match(js, /runTranslation\(\{ fromGesture: true \}\)/);
  // The manual control stays: a browser that refuses falls back, never to silence.
  assert.match(js, /speakButton\.addEventListener\("click"/);
});

test("responsive and reduced-motion treatments are present", () => {
  assert.match(css, /@media \(max-width: 760px\)/);
  assert.match(css, /prefers-reduced-motion/);
});
