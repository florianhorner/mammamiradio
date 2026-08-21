// Compose each scenario's beats[] into one continuous station segment.
//
// An operator command, not CI: rendering host voice costs money and the
// starter-catalog music lands with the modern-starter-crate branch, so this
// script runs on a maintainer machine after both exist. It is the single
// place segment audio is assembled, so the page, the README proof clip, and
// any future capture-replacement all share one recipe.
//
//   node scripts/produce-segments.mjs             # dry-run: validate + plan
//   node scripts/produce-segments.mjs --render    # assemble with ffmpeg
//
// Inputs per beat kind (from scenarios.mjs, the single source of truth):
//   tail     a starter-catalog song tail. File expected at
//            tmp/produce/tail/<scenarioId>.mp3 (cut by the operator from the
//            landed crate; CC-BY, the page carries the attribution line).
//   imaging  an id resolved against mammamiradio/assets/imaging/manifest.json
//            (CC0, checksum-bound).
//   voice    rendered host speech, expected at
//            tmp/produce/voice/<scenarioId>-<beatIndex>.mp3.
//
// Output: public/audio/<id>.mp3 plus public/audio/segments.manifest.json
// carrying per-clip sha256, durationSec, the composed source ids, and the
// MEASURED revealAtSec (the offset where the beat marked `moment: true`
// begins). Humans do not guess cue points; this script measures them, and
// scenarios.mjs is then updated to match. The manifest deliberately does NOT
// pin the producing commit: a squash would orphan it and turn a provenance
// claim into a lie (this repo has paid for that once already).

import { createHash } from "node:crypto";
import { execFile } from "node:child_process";
import { mkdir, readFile, writeFile, access } from "node:fs/promises";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { promisify } from "node:util";
import scenarios from "../scenarios.mjs";

const run = promisify(execFile);
const here = dirname(fileURLToPath(import.meta.url));
const explainerRoot = resolve(here, "..");
const repoRoot = resolve(explainerRoot, "../..");
const imagingRoot = join(repoRoot, "mammamiradio/assets/imaging");
const render = process.argv.includes("--render");

const imagingManifest = JSON.parse(await readFile(join(imagingRoot, "manifest.json"), "utf8"));
const imagingById = new Map();
for (const asset of Object.values(imagingManifest.assets ?? {})) {
  if (asset?.id && asset?.path) imagingById.set(asset.id, join(imagingRoot, asset.path));
}

function beatSourcePath(scenarioId, beat, index) {
  if (beat.kind === "imaging") return imagingById.get(beat.id) ?? null;
  if (beat.kind === "tail") return join(explainerRoot, "tmp/produce/tail", `${scenarioId}.mp3`);
  if (beat.kind === "voice") return join(explainerRoot, "tmp/produce/voice", `${scenarioId}-${index}.mp3`);
  return null;
}
async function exists(path) {
  try { await access(path); return true; } catch { return false; }
}
async function durationSec(path) {
  const { stdout } = await run("ffprobe", ["-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", path]);
  return Number.parseFloat(stdout.trim());
}

// Validate every beat before touching ffmpeg, and report the whole plan —
// a dry run that stops at the first gap would hide the rest of the work.
const plan = [];
let missing = 0;
for (const [id, scenario] of Object.entries(scenarios)) {
  const beats = [];
  for (const [index, beat] of scenario.beats.entries()) {
    if (beat.kind === "imaging" && !imagingById.has(beat.id)) {
      throw new Error(`Scenario "${id}" beat ${index} references imaging id "${beat.id}" that is not in the pack manifest`);
    }
    const source = beatSourcePath(id, beat, index);
    const present = source ? await exists(source) : false;
    if (!present) missing += 1;
    beats.push({ index, kind: beat.kind, moment: Boolean(beat.moment), source, present, note: beat.note ?? beat.id ?? "" });
  }
  if (!beats.some((beat) => beat.moment)) throw new Error(`Scenario "${id}" has no beat marked moment: true — nothing to cue the reveal on`);
  plan.push({ id, beats });
}

for (const { id, beats } of plan) {
  console.log(`\n${id}`);
  for (const beat of beats) {
    console.log(`  ${beat.present ? "ok " : "MISSING"}  ${beat.kind.padEnd(7)} ${beat.moment ? "← the home moment" : beat.note}`);
    if (!beat.present && beat.source) console.log(`           wants ${beat.source}`);
  }
}

if (!render) {
  console.log(`\nDry run: ${missing === 0 ? "all inputs present — run with --render" : `${missing} input file(s) missing (see above)`}.`);
  process.exit(0);
}
if (missing > 0) {
  console.error(`\nCannot render: ${missing} input file(s) missing. The dry run above names each one.`);
  process.exit(1);
}

// Assemble: concat the beats and measure where the moment beat begins.
// Plain concatenation on purpose — the imaging pack and voice renders are
// already levelled, and this page is a demo, not the egress chain.
const manifest = { generatedForPage: "docs/explainer", segments: {} };
await mkdir(join(explainerRoot, "public/audio"), { recursive: true });
for (const { id, beats } of plan) {
  let revealAtSec = null;
  let cursor = 0;
  for (const beat of beats) {
    if (beat.moment && revealAtSec === null) revealAtSec = Math.round(cursor * 100) / 100;
    cursor += await durationSec(beat.source);
  }
  const listPath = join(explainerRoot, "tmp/produce", `${id}.txt`);
  await mkdir(dirname(listPath), { recursive: true });
  await writeFile(listPath, beats.map((beat) => `file '${beat.source.replaceAll("'", "'\\''")}'`).join("\n"));
  const outPath = join(explainerRoot, "public/audio", `${id}.mp3`);
  await run("ffmpeg", ["-y", "-f", "concat", "-safe", "0", "-i", listPath, "-c:a", "libmp3lame", "-q:a", "4", outPath]);
  const bytes = await readFile(outPath);
  manifest.segments[id] = {
    sha256: createHash("sha256").update(bytes).digest("hex"),
    durationSec: Math.round((await durationSec(outPath)) * 100) / 100,
    revealAtSec,
    // The tail records its content hash: the page's music credit is
    // hard-coded to the tails' actual source, so a swapped tail must show
    // up in the manifest diff instead of silently outdating the credit.
    sources: await Promise.all(beats.map(async (beat) => {
      if (beat.kind === "imaging") return beat.note || beat.kind;
      if (beat.kind === "tail") {
        const tailSha = createHash("sha256").update(await readFile(beat.source)).digest("hex");
        return `tail:${tailSha.slice(0, 12)}`;
      }
      return `${beat.kind}:${beat.index}`;
    })),
  };
  console.log(`rendered ${id}: ${manifest.segments[id].durationSec}s, moment at ${revealAtSec}s`);
}
await writeFile(join(explainerRoot, "public/audio/segments.manifest.json"), `${JSON.stringify(manifest, null, 2)}\n`);
console.log("\nWrote public/audio/segments.manifest.json — now copy each revealAtSec into scenarios.mjs (the build guard will hold you to it).");
