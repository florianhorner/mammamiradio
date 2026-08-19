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
  assert.match(html, /Your smart home/);
  assert.match(html, /explained by the radio/);
  assert.match(html, /Make sense of this home/);
  assert.match(html, /What Home Assistant sees/);
  assert.match(html, /What a person understands/);
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
  assert.match(html, /combines Home Assistant states/);
});

test("responsive and reduced-motion treatments are present", () => {
  assert.match(css, /@media \(max-width: 760px\)/);
  assert.match(css, /prefers-reduced-motion/);
});
