import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import nextPhase from "../phase.mjs";

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

test("the idle state previews the station, not a sensor pipeline", () => {
  // The old idle teased sensor->meaning pairs, which taught the visitor the
  // page was a translator before they heard anything. The idle preview now
  // sells the show: hosts, ads, and only then the house.
  assert.match(html, /A show is already running/);
  assert.match(html, /Two hosts with history\./);
  assert.match(html, /Brands that do not exist\./);
  assert.match(html, /Your house, on air\./);
  assert.match(css, /@keyframes teaser-cycle/);
  assert.doesNotMatch(html, /A home moment is forming/);
});

test("audio position drives the reveal, not a stopwatch", () => {
  // Fixed motionTiming resolved the page at 2.9s; a 20s station segment
  // would then play into a page that had already finished. The phase machine
  // is a pure function fed by timeupdate, and the deadline exists only for
  // the silent case where playback never begins.
  assert.doesNotMatch(js, /motionTiming/);
  assert.match(js, /import nextPhase from "\.\/phase\.mjs"/);
  assert.match(js, /addEventListener\("timeupdate"/);
  assert.match(js, /revealAtSec: scenario\.revealAtSec/);
  assert.match(js, /AUDIO_DEADLINE_MS/);
  assert.match(css, /@keyframes sensor-lock/);
  assert.match(css, /@keyframes stage-sweep/);
  assert.match(css, /@keyframes on-air-rise/);
});

test("the phase machine follows aired-truth", () => {
  // Reported failure and the silent deadline both reveal — never a frozen
  // page — but always as audio:"failed", never as a successful moment.
  assert.deepEqual(nextPhase({ phase: "onair", failed: true }), { phase: "revealed", audio: "failed" });
  assert.deepEqual(nextPhase({ phase: "onair", deadline: true }), { phase: "revealed", audio: "failed" });
  // The cue point lands the reveal inside the clip.
  assert.deepEqual(nextPhase({ phase: "onair", positionSec: 14.2, revealAtSec: 14 }), { phase: "revealed", audio: "" });
  // Before the cue the show keeps playing unrevealed.
  assert.deepEqual(nextPhase({ phase: "onair", positionSec: 4, revealAtSec: 14 }), { phase: "onair", audio: "" });
  // Null cue (clips predating the produced manifest): reveal on ended.
  assert.deepEqual(nextPhase({ phase: "onair", positionSec: 6, revealAtSec: null }), { phase: "onair", audio: "" });
  assert.deepEqual(nextPhase({ phase: "onair", ended: true }), { phase: "revealed", audio: "" });
  // Buffering is loading, not failure.
  assert.deepEqual(nextPhase({ phase: "onair", positionSec: 4, revealAtSec: 14, buffering: true }), { phase: "onair", audio: "loading" });
  // Idle stays idle until a gesture starts the show.
  assert.deepEqual(nextPhase({ phase: "idle" }), { phase: "idle", audio: "" });
  // A revealed page never un-reveals.
  assert.deepEqual(nextPhase({ phase: "revealed", positionSec: 0 }), { phase: "revealed", audio: "" });
});

test("a failed clip never wears the on-air treatment", () => {
  assert.match(html, /id="audio-trouble" hidden/);
  assert.match(html, /You should have heard this:/);
  assert.match(js, /function reportFailure/);
  assert.match(js, /troubleTranscript\.textContent = scenario\.transcript/);
  assert.match(css, /body\[data-audio="failed"\] \.on-air-card/);
  assert.match(css, /body\[data-audio="failed"\] \.live-pill i \{ animation: none/);
  // The failure copy names the way forward, not just the problem.
  assert.match(js, /tap ▶/);
});

test("the gate is a transport, not a sensors-to-meaning pipeline", () => {
  // The old signal lines swept left to right from the sensor panel into the
  // meaning panel: a machine diagram of the framing this page no longer
  // makes. Playback position is the only thing the spine shows now.
  assert.doesNotMatch(html, /signal-line|signal-a|signal-b|signal-c/);
  assert.doesNotMatch(css, /signal-travel/);
  assert.match(html, /class="transport-track"/);
  assert.match(css, /\.transport-progress \{[^}]*height: var\(--progress, 0%\)/);
  assert.match(js, /transportProgress\.style\.setProperty\("--progress"/);
  assert.match(css, /body\[data-audio="loading"\] \.transport-track/);
});

test("the stage leads with the show and hides the answer until it airs", () => {
  // On-air panel before sensor panel, in DOM order — the inversion itself.
  assert.ok(html.indexOf('class="onair-panel"') < html.indexOf('class="sensor-panel"'), "the on-air panel must precede the sensor panel");
  assert.match(css, /grid-template-columns: minmax\(300px, 1\.14fr\) 104px minmax\(240px, \.86fr\)/);
  // The sensors are the reveal: face-down until the moment has aired.
  assert.match(css, /\.sensor-panel \.sensor-list[^{]*\{[^}]*opacity: 0/ms);
  assert.match(css, /body\[data-phase="revealed"\] \.sensor-panel \.sensor-list/);
  assert.match(html, /These are the liner notes, not the show\./);
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
  assert.match(css, /prefers-reduced-motion/);
  assert.match(html, /id="translation-announcement" role="status" aria-live="polite"/);
  assert.match(html, /aria-current="step"/);
  assert.match(js, /setAttribute\("aria-current", "step"\)/);
  // The transcript is announced once, at the reveal — not read over the clip.
  assert.match(js, /translationAnnouncement\.textContent = `\$\{scenario\.transcript\}/);
});
