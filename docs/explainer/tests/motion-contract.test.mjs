import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import nextPhase, { waitingLineIndex } from "../phase.mjs";
import scenarios, { transcriptFor } from "../scenarios.mjs";

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
  assert.match(css, /public\/fonts\/playfair-display-italic\.woff2/);
  assert.match(css, /public\/fonts\/outfit\.woff2/);
  // The mono face must actually load: --font-mono named JetBrains Mono for
  // weeks while no @font-face served it, so every entity id rendered in a
  // platform fallback. Self-hosted like the other faces (OFL licence).
  assert.match(css, /public\/fonts\/jetbrains-mono\.woff2/);
  assert.match(build, /dist\/public\/fonts\/jetbrains-mono\.woff2/);
  assert.match(build, /dist\/public\/fonts\/playfair-display\.woff2/);
});

test("the idle state previews the station, not a sensor pipeline", () => {
  // The old idle teased sensor->meaning pairs, which taught the visitor the
  // page was a translator before they heard anything. The idle preview now
  // sells the show: hosts, ads, and only then the house.
  assert.match(html, /The show is already on/);
  assert.match(html, /Two hosts who know each other too well\./);
  assert.match(html, /Companies best left imaginary\./);
  assert.match(html, /Your home, part of the show\./);
  assert.match(css, /@keyframes teaser-cycle/);
  assert.doesNotMatch(html, /A home moment is forming/);
});

test("audio position drives the reveal, not a stopwatch", () => {
  // Fixed motionTiming resolved the page at 2.9s; a 20s station segment
  // would then play into a page that had already finished. The phase machine
  // is a pure function fed by timeupdate, and the deadline exists only for
  // the silent case where playback never begins.
  assert.doesNotMatch(js, /motionTiming/);
  assert.match(js, /import nextPhase, \{ waitingLineIndex \} from "\.\/phase\.mjs"/);
  assert.match(js, /addEventListener\("timeupdate"/);
  assert.match(js, /revealAtSec: scenario\.revealAtSec/);
  assert.match(js, /AUDIO_DEADLINE_MS/);
  assert.match(css, /@keyframes on-air-rise/);
  // The reveal is ONE staggered rise. The per-row lock slides and the
  // full-card gold sweep fired on top of it and read as a glitch; they must
  // not come back.
  assert.doesNotMatch(css, /@keyframes sensor-lock|@keyframes stage-sweep/);
  assert.match(css, /\.sensor-panel \.sensor-list \{ transition-delay: 140ms; \}/);
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

test("the waiting line escalates on the cue's own clock", () => {
  // Pure decision, tested at its boundaries: opener until 45% of the way to
  // the moment, "the house knows" until 80%, "any second now" after.
  assert.equal(waitingLineIndex(0, 20), 0);
  assert.equal(waitingLineIndex(8.9, 20), 0);
  assert.equal(waitingLineIndex(9, 20), 1);
  assert.equal(waitingLineIndex(15.9, 20), 1);
  assert.equal(waitingLineIndex(16, 20), 2);
  // A null cue never escalates — there is no beat to pace against.
  assert.equal(waitingLineIndex(25, null), 0);
});

test("display copy carries no em dashes, in any layer", () => {
  // index.html has its own guard, but scenario display fields and the
  // failure copy are injected from JS and slipped past it once. Spoken-line
  // transcripts (lines[].text) keep their dashes: they transcribe speech.
  for (const scenario of Object.values(scenarios)) {
    for (const field of ["tag", "heading", "summary", "host", "quote", "time"]) {
      assert.ok(!String(scenario[field]).includes("—"), `${scenario.id}.${field} carries an em dash`);
    }
  }
  const uiStrings = [...js.matchAll(/"([^"\n]*)"/g)].map((m) => m[1]).filter((t) => /[a-z] [a-z]/i.test(t));
  for (const text of uiStrings) {
    assert.ok(!text.includes("—"), `app.js UI string carries an em dash: ${text.slice(0, 50)}`);
  }
});

test("the transcript is derived from the beats, never stored", () => {
  // The transcript is definitionally the voice beats' lines in air order;
  // storing a second copy let it drift from the audio. Derived, it cannot.
  for (const scenario of Object.values(scenarios)) {
    assert.equal("transcript" in scenario, false, "no stored transcript field");
    const derived = transcriptFor(scenario);
    assert.match(derived, /^Marco: |^Giulia: /);
    for (const beat of scenario.beats) {
      if (beat.kind !== "voice") continue;
      for (const line of beat.lines) assert.ok(derived.includes(line.text), "every aired line is in the transcript");
    }
  }
  assert.match(js, /transcriptFor\(scenario\)/);
  assert.doesNotMatch(js, /scenario\.transcript/);
});

test("a failed clip never wears the on-air treatment", () => {
  assert.match(html, /id="audio-trouble" hidden/);
  assert.match(html, /You should have heard this:/);
  assert.match(js, /function reportFailure/);
  assert.match(js, /troubleTranscript\.textContent = transcriptFor\(scenario\)/);
  assert.match(css, /body\[data-audio="failed"\] \.on-air-card/);
  assert.match(css, /body\[data-audio="failed"\] \.live-pill i \{ animation: none/);
  // The failure copy names the way forward, not just the problem.
  assert.match(js, /Tap ▶/);
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
  // The sensors are the reveal: face-down until the moment has aired —
  // present as shapes (an empty panel reads as a bug), unreadable as content,
  // with one story line over them so the wait is theater, not silence.
  assert.match(css, /\.sensor-panel \.sensor-list[^{]*\{[^}]*opacity: \.3/ms);
  assert.match(css, /filter: blur\(5px\)/);
  assert.match(html, /class="reveal-waiting"/);
  // Idle carries its own invitation; the story lines take over on tune-in.
  assert.match(html, /Tune in to hear what they notice\./);
  assert.match(js, /IDLE_WAITING_LINE/);
  assert.match(js, /let waitingIndex = -1/);
  // The waiting line escalates with the assembly, paced by playback
  // position against the cue — theater moves, it does not sit still.
  assert.match(js, /WAITING_LINES = \[/);
  assert.match(js, /The house knows something they don’t\./);
  assert.match(js, /Any second now…/);
  assert.match(js, /function updateWaitingLine/);
  // A screen reader must not get the answer before the show: the reveal
  // content enters the accessibility tree only when the moment airs — and
  // leaves it again on EVERY path into onair, not just the first (the
  // re-hide once lived only in reset, so scenario two spoiled the reveal).
  assert.match(html, /id="reveal-content" aria-hidden="true"/);
  assert.match(js, /revealContent\.removeAttribute\("aria-hidden"\)/);
  const rehides = js.match(/revealContent\.setAttribute\("aria-hidden", "true"\)/g);
  assert.ok(rehides && rehides.length >= 2, "aria re-hide must exist in both tuneIn and reset");
  // The deadline covers the stalled-mid-clip case, not only never-started.
  assert.match(js, /const armedAtSec = hostAudio\.currentTime/);
  assert.match(js, /neverStarted \|\| stalled/);
  assert.match(css, /body\[data-phase="revealed"\] \.reveal-waiting \{ visibility: hidden/);
  assert.match(css, /body\[data-phase="revealed"\] \.sensor-panel \.sensor-list/);
  // While the show plays, the reveal assembles itself behind the frost:
  // rows arrive one by one, the headline block arrives late. Every assembly
  // animation must end on the base ghost values and be dropped at the
  // reveal, so the sharpen transition takes over without a jump.
  assert.match(css, /@keyframes assemble-row/);
  assert.match(css, /@keyframes assemble-block/);
  assert.match(css, /body\[data-phase="onair"\] \.sensor-panel \.sensor-row:nth-child\(5\) \{ animation-delay: 11\.4s/);
  assert.match(css, /body\[data-phase="revealed"\] \.sensor-panel \.sensor-row[^{]*\{ animation: none/);
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
  assert.match(js, /translationAnnouncement\.textContent = `\$\{transcriptFor\(scenario\)\}/);
});
