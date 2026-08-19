import { copyFile, mkdir, readFile, writeFile } from "node:fs/promises";
import { runInNewContext } from "node:vm";

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

await mkdir("dist/public", { recursive: true });
await mkdir("dist/public/fonts", { recursive: true });
await mkdir("dist/public/audio", { recursive: true });
await Promise.all([
  ...audioIds.map((id) => copyFile(`public/audio/${id}.mp3`, `dist/public/audio/${id}.mp3`)),
  writeFile("dist/index.html", renderedIndex),
  copyFile("styles.css", "dist/styles.css"),
  copyFile("app.js", "dist/app.js"),
  copyFile(".nojekyll", "dist/.nojekyll"),
  copyFile("public/logo.svg", "dist/public/logo.svg"),
  copyFile("public/favicon.svg", "dist/public/favicon.svg"),
  copyFile("public/icon-192.svg", "dist/public/icon-192.svg"),
  copyFile("public/fonts/playfair-display.ttf", "dist/public/fonts/playfair-display.ttf"),
  copyFile("public/fonts/playfair-display-italic.ttf", "dist/public/fonts/playfair-display-italic.ttf"),
  copyFile("public/fonts/outfit.ttf", "dist/public/fonts/outfit.ttf"),
]);
console.log("Built local static site in dist/");
