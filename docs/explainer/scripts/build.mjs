import { copyFile, mkdir, readFile, writeFile } from "node:fs/promises";
import { runInNewContext } from "node:vm";
import scenarios from "../scenarios.mjs";

const indexTemplate = await readFile("index.html", "utf8");
const configMatch = indexTemplate.match(/window\.mammamiSiteLinks\s*=\s*({[\s\S]*?});/);
if (!configMatch) throw new Error("Missing mammamiSiteLinks configuration in index.html");
const siteLinks = runInNewContext(`(${configMatch[1]})`);
const escapeHtml = (value) => String(value).replace(/[&<>\"]/g, (character) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "\"": "&quot;" }[character]));
const renderedSiteLinks = Object.values(siteLinks)
  .filter((link) => link?.href && link?.label)
  .map((link) => `<a href="${escapeHtml(link.href)}" target="_blank" rel="noreferrer noopener">${escapeHtml(link.label)}</a>`)
  .join("");
const renderedIndex = indexTemplate.replace(
  /<nav id="site-links" class="footer-links" aria-label="Project links"><\/nav>/,
  `<nav id="site-links" class="footer-links" aria-label="Project links">${renderedSiteLinks}</nav>`,
);
if (renderedIndex === indexTemplate) throw new Error("Missing site-links footer slot in index.html");

// Every scenario needs its host clip in the build. The copy list below is
// explicit by design, which means a fifth scenario without a clip would ship a
// play button pointing at a 404 — so the ids are derived from the same source
// the page uses, and a missing file fails the build instead of the visitor.
const scenarioIds = [...indexTemplate.matchAll(/data-scenario-id="([a-z]+)"/g)].map((match) => match[1]);
const audioIds = [...new Set(scenarioIds)];
if (audioIds.length === 0) throw new Error("No scenario ids found in index.html");

// scenarios.mjs is the single source of truth; the picker buttons in
// index.html must agree with it in both directions, and at least one moment
// must be reachable by a fresh install (narrow ambient context: sun and
// weather only). A page whose every demo needs a home grant oversells.
const truthIds = Object.keys(scenarios);
for (const id of audioIds) {
  if (!truthIds.includes(id)) throw new Error(`index.html offers "${id}" but scenarios.mjs does not define it`);
}
for (const id of truthIds) {
  if (!audioIds.includes(id)) throw new Error(`scenarios.mjs defines "${id}" but index.html never offers it`);
}
if (!truthIds.some((id) => scenarios[id].reachability === "day-one")) {
  throw new Error("No scenario is fresh-install reachable (reachability: \"day-one\") — the page would demonstrate only gated capability");
}
// Every scenario must carry what a visitor who cannot hear the clip needs,
// and a cue point may only exist alongside the produced manifest that
// measured it — humans do not guess cue points. Once produce-segments.mjs
// emits segments.manifest.json, a null revealAtSec becomes a build failure
// too, so the page cannot ship produced audio without its reveal landing.
let producedManifest = null;
try {
  producedManifest = JSON.parse(await readFile("public/audio/segments.manifest.json", "utf8"));
} catch {
  // No produced manifest yet: pre-producer clips reveal on `ended`.
}
for (const id of truthIds) {
  const scenario = scenarios[id];
  if (!scenario.transcript || !scenario.transcript.trim()) {
    throw new Error(`Scenario "${id}" has no transcript — a visitor who cannot hear the clip would get nothing`);
  }
  const produced = producedManifest?.segments?.[id];
  if (produced) {
    if (scenario.revealAtSec === null) throw new Error(`Scenario "${id}" has a produced clip but no revealAtSec cue point`);
    if (typeof produced.durationSec !== "number" || scenario.revealAtSec >= produced.durationSec) {
      throw new Error(`Scenario "${id}" revealAtSec (${scenario.revealAtSec}) must fall inside the produced clip (${produced.durationSec}s)`);
    }
  } else if (scenario.revealAtSec !== null && typeof scenario.revealAtSec !== "number") {
    throw new Error(`Scenario "${id}" revealAtSec must be a number or null`);
  }
}

await mkdir("dist/public", { recursive: true });
await mkdir("dist/public/fonts", { recursive: true });
await mkdir("dist/public/audio", { recursive: true });
await Promise.all([
  ...audioIds.map((id) => copyFile(`public/audio/${id}.mp3`, `dist/public/audio/${id}.mp3`)),
  writeFile("dist/index.html", renderedIndex),
  copyFile("styles.css", "dist/styles.css"),
  copyFile("app.js", "dist/app.js"),
  copyFile("scenarios.mjs", "dist/scenarios.mjs"),
  copyFile(".nojekyll", "dist/.nojekyll"),
  copyFile("public/logo.svg", "dist/public/logo.svg"),
  copyFile("public/share-card.png", "dist/public/share-card.png"),
  copyFile("public/favicon.svg", "dist/public/favicon.svg"),
  copyFile("public/icon-192.svg", "dist/public/icon-192.svg"),
  copyFile("public/fonts/playfair-display.ttf", "dist/public/fonts/playfair-display.ttf"),
  copyFile("public/fonts/playfair-display-italic.ttf", "dist/public/fonts/playfair-display-italic.ttf"),
  copyFile("public/fonts/outfit.ttf", "dist/public/fonts/outfit.ttf"),
  copyFile("public/fonts/jetbrains-mono.ttf", "dist/public/fonts/jetbrains-mono.ttf"),
]);
console.log("Built local static site in dist/");
