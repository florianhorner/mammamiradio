// The funnel, executed: tune in -> the reveal fires at the cue -> the install
// CTA takes the gold -> after all four moments the hero button retires. The
// string-pin tests guard the source; this one drives the real page in a real
// browser, because a refactor can keep every pin green while breaking the
// actual click-through. Runs via `npm run test:e2e` (needs `npx playwright
// install chromium` once); deliberately NOT part of plain `npm test`, which
// stays dependency-free.
//
// Clips are the real ~30s segments; the test seeks the audio (via the
// __mmrTest hook in app.js) instead of listening in real time, so the whole
// funnel executes in seconds.

import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import test from "node:test";
import { chromium } from "playwright";

// Per-run port: an orphaned server from a timed-out run otherwise squats the
// fixed port, the new spawn dies silently on EADDRINUSE, and waitForServer
// happily talks to the stale orphan.
const PORT = 4900 + (process.pid % 90);
const BASE = `http://127.0.0.1:${PORT}/`;

let server;
let browser;
let page;

async function waitForServer() {
  for (let attempt = 0; attempt < 40; attempt += 1) {
    try {
      const response = await fetch(BASE);
      if (response.ok) return;
    } catch {
      // not up yet
    }
    await new Promise((resolve) => setTimeout(resolve, 250));
  }
  throw new Error("preview server never came up");
}

test.before(async () => {
  server = spawn(process.execPath, ["server.mjs"], { env: { ...process.env, PORT: String(PORT) }, stdio: "ignore" });
  await waitForServer();
  browser = await chromium.launch();
  page = await browser.newPage();
  // Reduced motion, for two reasons: the idle CTA deliberately never stops
  // moving (breath + beckon), which Playwright's stability check refuses to
  // click — and this way CI exercises the page's reduced-motion contract on
  // every run instead of never.
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.goto(BASE);
});

test.after(async () => {
  await browser?.close();
  server?.kill();
});

const phase = () => page.getAttribute("body", "data-phase");
const seekPast = async (seconds) => {
  // Seeking before the clip's metadata loads gets clamped back to 0 and the
  // clip plays in real time — wait for HAVE_METADATA first.
  await page.waitForFunction(() => globalThis.__mmrTest.audio.readyState >= 1);
  await page.evaluate((s) => { globalThis.__mmrTest.audio.currentTime = s; }, seconds);
};
const cueFor = (id) => page.evaluate(async (scenarioId) => {
  const { default: scenarios } = await import("./scenarios.mjs");
  return scenarios[scenarioId].revealAtSec;
}, id);

test("tune in starts the show and the answer stays hidden until the cue", async () => {
  assert.equal(await phase(), "idle");
  assert.equal(await page.getAttribute("#reveal-content", "aria-hidden"), "true");
  await page.click("#translate-button");
  await page.waitForFunction(() => document.body.dataset.phase === "onair");
  // Before the cue: still on air, answer still out of the a11y tree.
  assert.equal(await page.getAttribute("#reveal-content", "aria-hidden"), "true");
  const cue = await cueFor("arrival");
  await seekPast(cue + 0.2);
  await page.waitForFunction(() => document.body.dataset.phase === "revealed");
  // The reveal: quote visible, answer joins the a11y tree, install CTA rises.
  assert.equal(await page.getAttribute("#reveal-content", "aria-hidden"), null);
  assert.equal(await page.isHidden("#host-quote"), false);
  await page.waitForFunction(() => getComputedStyle(document.querySelector("#install-cta")).visibility === "visible");
  const href = await page.getAttribute("#install-cta", "href");
  assert.match(href, /my\.home-assistant\.io/);
});

test("the no-spoiler promise holds on every scenario, not just the first", async () => {
  // E1 regression, executed: switching scenarios mid-run must re-hide the
  // answer from assistive tech during the new clip's on-air stretch.
  await page.click('[data-scenario-id="coffee"]');
  await page.waitForFunction(() => document.body.dataset.phase === "onair");
  assert.equal(await page.getAttribute("#reveal-content", "aria-hidden"), "true");
  const cue = await cueFor("coffee");
  await seekPast(cue + 0.2);
  await page.waitForFunction(() => document.body.dataset.phase === "revealed");
  assert.equal(await page.getAttribute("#reveal-content", "aria-hidden"), null);
});

test("after the last unheard moment the hero button retires", async () => {
  for (const id of ["laundry", "quiet"]) {
    await page.click(`[data-scenario-id="${id}"]`);
    await page.waitForFunction(() => document.body.dataset.phase === "onair");
    const cue = await cueFor(id);
    await seekPast(cue + 0.2);
    await page.waitForFunction(() => document.body.dataset.phase === "revealed");
  }
  // All four heard: "Hear another one" would be a lie, so it is gone and the
  // install CTA is the only ask left standing.
  assert.equal(await page.isHidden("#translate-button"), true);
  // Computed-style check rather than page.isVisible(): Playwright reported
  // false here while the element was provably flex/visible/opacity-1 with a
  // real box. What we mean is CSS visibility, so assert exactly that.
  // waitForFunction, not an instant assert: visibility transitioning from
  // hidden reads as hidden at transition progress 0, the exact tick the
  // phase flips. One frame later it is visible; wait for that frame.
  await page.waitForFunction(() => {
    const cta = document.querySelector("#install-cta");
    const style = getComputedStyle(cta);
    return style.display !== "none" && style.visibility === "visible" && cta.getBoundingClientRect().width > 0;
  }, undefined, { timeout: 5000 });
});

test("start over returns the page to an honest idle", async () => {
  await page.click("#reset-button");
  await page.waitForFunction(() => document.body.dataset.phase === "idle");
  assert.equal(await page.getAttribute("#reveal-content", "aria-hidden"), "true");
  assert.equal(await page.isHidden("#translate-button"), false);
  const waiting = await page.textContent(".reveal-waiting p");
  assert.equal(waiting, "Tune in to hear what they notice.");
});
