// The scenario data lives in scenarios.mjs — the single source of truth the
// page, the clip producer, and the build guards all read. The phase decisions
// live in phase.mjs as a pure function so they are testable without a browser.
import scenarios from "./scenarios.mjs";
import nextPhase from "./phase.mjs";

const siteLinks = globalThis.mammamiSiteLinks ?? {};

const body = document.body;
const translateButton = document.querySelector("#translate-button");
const translateLabel = translateButton.querySelector("span");
const resetButton = document.querySelector("#reset-button");
const speakButton = document.querySelector("#speak-button");
const gateLabel = document.querySelector("#gate-label");
const transportProgress = document.querySelector("#transport-progress");
const translationAnnouncement = document.querySelector("#translation-announcement");
const siteLinksNav = document.querySelector("#site-links");
const hostQuote = document.querySelector("#host-quote");
const audioTrouble = document.querySelector("#audio-trouble");
const troubleLine = document.querySelector("#trouble-line");
const troubleTranscript = document.querySelector("#trouble-transcript");
const sensorRows = [...document.querySelectorAll(".sensor-row")];
const scenarioButtons = [...document.querySelectorAll(".scenario-button")];
const journeySteps = [...document.querySelectorAll(".journey li")];
const scenarioIds = Object.keys(scenarios);
let activeScenario = "arrival";
let deadlineTimer = null;
let isSpeaking = false;
// Longer than the longest produced clip: the deadline exists only for the
// silent case where playback never begins and no event ever fires.
const AUDIO_DEADLINE_MS = 30000;
const waveformShape = [0.34, 0.62, 0.82, 0.48, 0.95, 0.68, 0.42, 0.88, 0.58, 1, 0.73, 0.38];

function renderSiteLinks() {
  if (!siteLinksNav) return;
  siteLinksNav.replaceChildren();
  Object.values(siteLinks).forEach((link) => {
    if (!link?.href || !link.label) return;
    const anchor = document.createElement("a");
    anchor.href = link.href;
    anchor.target = "_blank";
    anchor.rel = "noreferrer noopener";
    anchor.textContent = link.label;
    siteLinksNav.append(anchor);
  });
}
function updateJourney(activeStep) {
  const order = ["onair", "reveal"];
  const activeIndex = order.indexOf(activeStep);
  journeySteps.forEach((step, index) => {
    const isCurrent = index === activeIndex;
    step.classList.toggle("is-current", isCurrent);
    step.classList.toggle("is-complete", index < activeIndex);
    if (isCurrent) step.setAttribute("aria-current", "step");
    else step.removeAttribute("aria-current");
  });
}
function initWaveforms() {
  document.querySelectorAll("[data-waveform]").forEach((waveform) => {
    const isHero = waveform.dataset.waveform === "hero";
    const count = isHero ? 24 : 16;
    const maxHeight = isHero ? 54 : 22;
    const bars = Array.from({ length: count }, (_, index) => {
      const bar = document.createElement("i");
      const shape = waveformShape[index % waveformShape.length];
      bar.className = "waveform-bar";
      bar.style.setProperty("--h", `${Math.round(maxHeight * shape)}px`);
      bar.style.setProperty("--d", `${680 + ((index * 73) % 440)}ms`);
      bar.style.setProperty("--dl", `${-((index * 61) % 520)}ms`);
      return bar;
    });
    waveform.replaceChildren(...bars);
  });
}
function paintScenario(id) {
  const scenario = scenarios[id];
  activeScenario = id;
  body.dataset.scenario = id;
  document.documentElement.style.setProperty("--scenario", scenario.color);
  document.documentElement.style.setProperty("--scenario-rgb", scenario.rgb);
  document.querySelector("#moment-time").textContent = scenario.time;
  document.querySelector("#moment-tag").textContent = scenario.tag;
  document.querySelector("#moment-heading").textContent = scenario.heading;
  document.querySelector("#moment-summary").textContent = scenario.summary;
  document.querySelector("#host-name").textContent = scenario.host;
  hostQuote.textContent = `“${scenario.quote}”`;
  sensorRows.forEach((row, index) => {
    const [icon, name, value] = scenario.sensors[index];
    row.querySelector(".sensor-icon").textContent = icon;
    row.querySelector(".sensor-name").textContent = name;
    row.querySelector(".sensor-value").textContent = value;
  });
  scenarioButtons.forEach((button) => {
    const active = button.dataset.scenarioId === id;
    button.classList.toggle("is-active", active);
    button.setAttribute("aria-pressed", String(active));
  });
}

// The hosts, not the browser. These are real renders of Marco and Giulia in
// the station's own configured voices, so the demo sounds like the product
// instead of teaching every visitor that it is a speech engine. Relative
// paths keep the page working on a GitHub Pages project subpath.
const hostAudio = new Audio();
hostAudio.preload = "none";
function audioSrcFor(scenarioId) { return `public/audio/${scenarioId}.mp3`; }

function applyState({ phase, audio }) {
  const wasRevealed = body.dataset.phase === "revealed";
  body.dataset.phase = phase;
  body.dataset.audio = audio;
  if (phase === "revealed" && !wasRevealed) onReveal(audio);
}
function clearDeadline() { if (deadlineTimer) { window.clearTimeout(deadlineTimer); deadlineTimer = null; } }
function armDeadline() {
  // The one failure the browser never reports: playback that silently never
  // begins. Any real progress re-arms the timer, so a slow download on a
  // thin connection reads as loading, never as failure.
  clearDeadline();
  deadlineTimer = window.setTimeout(() => {
    if (body.dataset.phase === "onair" && hostAudio.currentTime === 0) {
      applyState(nextPhase({ phase: "onair", deadline: true }));
    }
  }, AUDIO_DEADLINE_MS);
}

function stopSpeech() {
  hostAudio.pause();
  isSpeaking = false;
  speakButton.classList.remove("is-speaking");
  speakButton.innerHTML = '<span aria-hidden="true">▶</span> Listen again';
}
function startSpeaking() {
  isSpeaking = true;
  speakButton.classList.add("is-speaking");
  speakButton.innerHTML = '<span aria-hidden="true">■</span> Stop';
}
function primeAudio(scenarioId) {
  // Start fetching the clip early so the voice begins the instant it is
  // asked for rather than after a download.
  const src = audioSrcFor(scenarioId);
  if (hostAudio.src.endsWith(src)) return;
  hostAudio.src = src;
  hostAudio.load();
}
// The single play path. Every control routes through here, so cue points and
// failure handling exist exactly once. Must be called from inside a user
// gesture handler for autoplay rules to allow it; if a browser still
// refuses, the rejection routes to the failed state rather than to silence.
function playSegment(scenarioId) {
  const src = audioSrcFor(scenarioId);
  if (!hostAudio.src.endsWith(src)) hostAudio.src = src;
  hostAudio.currentTime = 0;
  const played = hostAudio.play();
  if (played && typeof played.catch === "function") {
    played.then(startSpeaking).catch(() => { stopSpeech(); reportFailure("blocked"); });
  } else {
    startSpeaking();
  }
}
function reportFailure(kind) {
  clearDeadline();
  stopSpeech();
  const scenario = scenarios[activeScenario];
  // Aired-truth, applied to the page: a visitor who heard nothing is never
  // shown a successful on-air moment. The reveal still fires — a frozen page
  // is the other broken promise — but the card says what should have been
  // heard, as text, and names the way forward.
  troubleLine.textContent = kind === "blocked"
    ? "Your browser held the audio back — tap ▶ to hear it. You should have heard:"
    : "The clip is catching its breath — try ▶ again in a moment. You should have heard:";
  troubleTranscript.textContent = scenario.transcript;
  audioTrouble.hidden = false;
  gateLabel.textContent = "Audio held back";
  applyState(nextPhase({ phase: body.dataset.phase === "idle" ? "onair" : body.dataset.phase, failed: true }));
}
function onReveal(audio) {
  clearDeadline();
  const scenario = scenarios[activeScenario];
  translateButton.disabled = false;
  translateLabel.textContent = "Hear another one";
  if (audio !== "failed") {
    gateLabel.textContent = "That was your house";
    hostQuote.hidden = false;
    audioTrouble.hidden = true;
  }
  // The transcript is announced once, at the reveal — not read over the clip.
  translationAnnouncement.textContent = `${scenario.transcript} ${scenario.heading} ${scenario.summary}`;
  updateJourney("reveal");
  revealResult();
}
hostAudio.addEventListener("ended", () => {
  stopSpeech();
  applyState(nextPhase({ phase: body.dataset.phase, ended: true }));
});
hostAudio.addEventListener("error", () => reportFailure("missing"));
hostAudio.addEventListener("timeupdate", () => {
  const scenario = scenarios[activeScenario];
  const duration = Number.isFinite(hostAudio.duration) ? hostAudio.duration : 0;
  if (duration > 0 && transportProgress) {
    transportProgress.style.setProperty("--progress", `${Math.min(100, (hostAudio.currentTime / duration) * 100)}%`);
  }
  if (hostAudio.currentTime > 0) armDeadline();
  applyState(nextPhase({ phase: body.dataset.phase, positionSec: hostAudio.currentTime, revealAtSec: scenario.revealAtSec }));
});
hostAudio.addEventListener("waiting", () => {
  if (body.dataset.phase === "idle") return;
  body.dataset.audio = "loading";
  gateLabel.textContent = "Tuning…";
});
hostAudio.addEventListener("playing", () => {
  if (body.dataset.audio === "loading") body.dataset.audio = "";
  if (body.dataset.phase === "onair") gateLabel.textContent = "On air";
});

// Where the mobile experience lives or dies: on a narrow viewport the panels
// stack, which puts the reveal below the fold. Bring it to the reader when it
// is off-screen, and only then, so the desktop layout is untouched.
function revealResult() {
  const panel = document.querySelector(".sensor-panel");
  if (!panel) return;
  const box = panel.getBoundingClientRect();
  if (box.top >= 0 && box.top < window.innerHeight * 0.75) return;
  const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  panel.scrollIntoView({ behavior: reduced ? "auto" : "smooth", block: "start" });
}
// Tune in: the first click starts the show. The voice IS the product;
// everything else on the page is setup for it, so it plays on this click,
// not a later one.
function tuneIn() {
  stopSpeech();
  hostQuote.hidden = true;
  audioTrouble.hidden = true;
  primeAudio(activeScenario);
  playSegment(activeScenario);
  body.dataset.audio = "";
  body.dataset.phase = "onair";
  translateButton.disabled = true;
  translateLabel.textContent = "Listening…";
  gateLabel.textContent = "On air";
  translationAnnouncement.textContent = "Tuned in. The station is on air.";
  resetButton.hidden = false;
  updateJourney("onair");
  armDeadline();
}
function resetExperience() {
  clearDeadline(); stopSpeech();
  body.dataset.phase = "idle"; body.dataset.audio = "";
  hostQuote.hidden = true; audioTrouble.hidden = true;
  translateButton.disabled = false;
  translateLabel.textContent = "Tune in"; gateLabel.textContent = "Ready";
  translationAnnouncement.textContent = ""; resetButton.hidden = true;
  if (transportProgress) transportProgress.style.setProperty("--progress", "0%");
  updateJourney("onair");
}
translateButton.addEventListener("click", () => {
  if (body.dataset.phase === "revealed") {
    const currentIndex = scenarioIds.indexOf(activeScenario);
    paintScenario(scenarioIds[(currentIndex + 1) % scenarioIds.length]);
  }
  tuneIn();
});
resetButton.addEventListener("click", resetExperience);
scenarioButtons.forEach((button) => {
  button.setAttribute("aria-pressed", String(button.classList.contains("is-active")));
  // Picking a different moment mid-demo is a user gesture too, so it plays.
  button.addEventListener("click", () => {
    const id = button.dataset.scenarioId;
    if (!id || id === activeScenario) return;
    paintScenario(id);
    if (body.dataset.phase !== "idle") tuneIn();
  });
});
speakButton.addEventListener("click", () => {
  if (isSpeaking) { stopSpeech(); return; }
  playSegment(activeScenario);
});
initWaveforms();
paintScenario(activeScenario);
renderSiteLinks();
