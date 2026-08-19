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
