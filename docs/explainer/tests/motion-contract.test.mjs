import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const [html, css, js, build] = await Promise.all([
  readFile("index.html", "utf8"),
  readFile("styles.css", "utf8"),
  readFile("app.js", "utf8"),
  readFile("scripts/build.mjs", "utf8"),
]);

test("the canonical radio identity is used instead of a standalone Mi emblem", () => {
  assert.match(html, /href="public\/favicon\.svg"/);
  assert.match(html, /src="public\/icon-192\.svg"/);
  assert.match(html, /src="public\/logo\.svg"/);
  assert.doesNotMatch(html, /(?:href|src)="\//);
  assert.doesNotMatch(html, /gate-orbit[^>]*>\s*<span>\s*MI\s*<\/span>/i);
  assert.match(css, /--bg: #14110f/);
  assert.match(css, /--flag-green: #009246/);
  assert.match(css, /"Playfair Display"/);
  assert.match(css, /Outfit/);
  assert.match(css, /public\/fonts\/playfair-display-italic\.ttf/);
  assert.match(css, /public\/fonts\/outfit\.ttf/);
  assert.match(build, /dist\/public\/fonts\/playfair-display\.ttf/);
});

test("the idle state previews the translation before the first click", () => {
  assert.match(html, /A home moment is forming/);
  assert.match(html, /Someone came home\./);
  assert.match(html, /Coffee just started\./);
  assert.match(html, /The laundry was forgotten\./);
  assert.match(css, /@keyframes teaser-cycle/);
  assert.match(css, /body\[data-phase="idle"\] \.sensor-row::after/);
});

test("the reveal preserves enough time for each visual phase to register", () => {
  assert.match(js, /motionTiming = \{ translating: 1200, speaking: 2900 \}/);
  assert.match(js, /Listening to the house…/);
  assert.match(js, /Finding the moment…/);
  assert.match(js, /gateLabel\.textContent = "On air"/);
  assert.match(css, /@keyframes sensor-lock/);
  assert.match(css, /@keyframes stage-sweep/);
  assert.match(css, /@keyframes on-air-rise/);
});

test("both teaser and on-air waveform variants are generated", () => {
  assert.match(html, /data-waveform="hero"/);
  assert.match(html, /data-waveform="strip"/);
  assert.match(js, /const count = isHero \? 24 : 16/);
  assert.match(js, /waveform\.replaceChildren\(\.\.\.bars\)/);
  assert.match(css, /@keyframes waveform-pulse/);
});

test("motion alternatives and progress remain understandable", () => {
  assert.match(css, /\.teaser-stack p:first-child \{ opacity: 1; \}/);
  assert.match(css, /\.waveform-bar \{ animation: none !important; height: var\(--h, 22px\)/);
  assert.match(css, /grid-template-columns: minmax\(240px, \.86fr\) 104px/);
  assert.match(css, /\.translation-gate \{[^}]*overflow: hidden/);
  assert.match(html, /id="translation-announcement" role="status" aria-live="polite"/);
  assert.match(html, /aria-current="step"/);
  assert.match(js, /setAttribute\("aria-current", "step"\)/);
  assert.match(js, /translationAnnouncement\.textContent = `\$\{scenario\.heading\}/);
});
