import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const [html, css, js, scenariosSource] = await Promise.all([
  readFile("index.html", "utf8"),
  readFile("styles.css", "utf8"),
  readFile("app.js", "utf8"),
  readFile("scenarios.mjs", "utf8"),
]);
const addonUrl = "https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Fflorianhorner%2Fmammamiradio";
const sourceUrl = "https://github.com/florianhorner/mammamiradio";
const escapeRegExp = (value) => value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

test("the signature promise is in the first viewport", () => {
  assert.match(html, /radio station/i);
  assert.match(html, /listens to the house/i);
  assert.match(html, /Tune in/);
  assert.match(html, /What you hear first/);
  assert.match(html, /What the hosts noticed/);
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

  // The meta description is the framing search engines and link previews
  // quote; it slid back to sensor-first once while every guard watched the
  // <h1>. It names the station and never leads with sensor translation.
  const meta = html.match(/<meta name="description" content="([^"]*)"/)[1];
  assert.match(meta, /radio station/i, "the meta description names the station");
  assert.doesNotMatch(meta, /sensor states|sensor data|plain-English/i, "the meta description is not about sensor translation");

  // Ordinal steps read as a pipeline whatever their labels say.
  assert.doesNotMatch(html, /class="flow-number"/, "the flow steps carry no ordinals");
  assert.doesNotMatch(html, /class="panel-number"/, "the stage panels carry no ordinals");

  // The old thesis sentence is the most translator-ish line the page ever had.
  assert.doesNotMatch(html, /Sensor data, finally understandable/);

  // The inversion itself: you hear the show before you see the sensors.
  assert.ok(html.indexOf('class="onair-panel"') < html.indexOf('class="sensor-panel"'), "the on-air panel precedes the sensor panel");
});

test("the experience has multiple interactive home moments", () => {
  for (const id of ["arrival", "coffee", "laundry", "quiet"]) {
    assert.match(html, new RegExp(`data-scenario-id="${id}"`));
    assert.match(scenariosSource, new RegExp(`${id}: \\{`));
  }
  // The data lives in scenarios.mjs and only there; app.js imports it.
  assert.match(js, /import scenarios from "\.\/scenarios\.mjs"/);
  assert.doesNotMatch(js, /binary_sensor\.front_door/, "sensor data must not creep back into app.js");
});

test("no scenario accent is green", () => {
  // The scenario accent drives the entire on-air chrome (live dot, waves,
  // transport, borders). Green is banned twice in docs/design/system.md:
  // the owner is red-green colorblind. #9cab7e shipped once; never again.
  assert.doesNotMatch(scenariosSource, /#9cab7e/i);
  assert.doesNotMatch(css, /--sage/);
});

test("at least one moment is reachable by a fresh install", () => {
  // Narrow ambient context projects only the sun and the weather
  // (home/authorization.py). A page whose every demo needs a home grant
  // sells a stranger something their install cannot do. This guard keeps
  // the day-one scenario from being silently traded away.
  assert.match(scenariosSource, /reachability: "day-one"/);
  const dayOneBlocks = scenariosSource.match(/reachability: "day-one"/g);
  assert.ok(dayOneBlocks.length >= 1);
  // The day-one moment may only use ambient entities.
  const quietBlock = scenariosSource.slice(scenariosSource.indexOf("quiet: {"));
  const sensorNames = [...quietBlock.matchAll(/\["[^"]*", "([^"]+)"/g)].map((m) => m[1]);
  for (const name of sensorNames.slice(0, 5)) {
    assert.match(name, /^(sun\.sun|weather\.home)/, `day-one scenario uses non-ambient entity: ${name}`);
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
  assert.match(html, /class="hero-intro">Marco and Giulia/);
});

test("the voice plays on the first click, not the second", () => {
  // The voice is the only part of this page that is not copyable, and it used
  // to sit behind a second click and a 2.9s animation, so a visitor could read
  // the whole page and never hear it. tuneIn calls playSegment inside the
  // button's own click handler, which is what makes autoplay permissible —
  // and playSegment is the ONLY play path, so cue points and failure handling
  // exist exactly once.
  assert.match(js, /function playSegment/);
  assert.match(js, /function tuneIn/);
  assert.match(js, /translateButton\.addEventListener\("click"/);
  const playCalls = js.match(/hostAudio\.play\(\)/g);
  assert.equal(playCalls.length, 1, "one play path, not a duplicated one");
  // The manual control stays: a browser that refuses falls back, never to silence.
  assert.match(js, /speakButton\.addEventListener\("click"/);
});

test("the day-one boundary is said in plain words", () => {
  assert.match(html, /On day one the station knows the sky\./);
  assert.match(html, /day-one-chip/);
  assert.match(html, /class="aired-truth"/);
});

test("exactly two calls to action carry the gold", () => {
  // The page's funnel is two clicks: Tune in to get going, then one click
  // into the Home Assistant install path. The install CTA appears with the
  // reveal (the page earns the ask first), takes the gold, and the hero
  // button demotes so the golds never compete.
  assert.match(html, /id="install-cta" hidden/);
  assert.match(js, /function wireInstallCta/);
  assert.match(js, /cta\.href = addon\.href/);
  // The install button is in layout from the start (visibility, not
  // display), so its reveal fades in without reflowing the row.
  assert.match(css, /\.install-cta \{[^}]*display: inline-flex; visibility: hidden/);
  assert.match(css, /body\[data-phase="revealed"\] \.install-cta:not\(\[hidden\]\) \{ visibility: visible; opacity: 1/);
  // Post-reveal both CTAs are gold and one spotlight alternates between
  // them: same keyframe, offset half a cycle, both starting only after the
  // entrance settles, with rest frames equal to the base shadow so the
  // animation starting is invisible.
  assert.match(css, /@keyframes cta-spotlight/);
  assert.match(css, /\.primary-button:not\(\[hidden\]\) \{ animation: cta-spotlight 4\.8s ease-in-out 600ms infinite/);
  assert.match(css, /\.install-cta:not\(\[hidden\]\) \{[^}]*animation: cta-spotlight 4\.8s ease-in-out 3s infinite/);
  assert.doesNotMatch(css, /revealed"\] \.primary-button \{ background: transparent/);
  assert.ok(html.indexOf('id="translate-button"') < html.indexOf('id="install-cta"'), "install sits beside the hero button, after it");
  assert.match(css, /body\[data-phase="idle"\] \.primary-button \{ animation: cta-beckon/);
  // After the last unheard moment, "Hear another one" would be a lie —
  // the button retires and install is the only ask left standing.
  assert.match(js, /playedScenarios\.size >= scenarioIds\.length/);
  assert.match(js, /translateButton\.hidden = true/);
});

test("the starter-catalog music tail carries its attribution", () => {
  // The segment openers are cut from the CC BY 4.0 starter catalog; using
  // the music obliges the credit line. If the tails ever leave the clips,
  // this guard is the reminder that the line can leave with them.
  assert.match(html, /Kevin MacLeod \(incompetech\.com\), licensed under CC BY 4\.0\./);
});

test("responsive and reduced-motion treatments are present", () => {
  assert.match(css, /@media \(max-width: 760px\)/);
  assert.match(css, /prefers-reduced-motion/);
});
