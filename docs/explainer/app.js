// The scenario data lives in scenarios.mjs — the single source of truth the
// page, the clip producer, and the build guards all read. Nothing about a
// moment (copy, sensors, transcript, cue point) is defined in this file.
import scenarios from "./scenarios.mjs";

const siteLinks = globalThis.mammamiSiteLinks ?? {};

const body = document.body;
const translateButton = document.querySelector("#translate-button");
const translateLabel = translateButton.querySelector("span");
const resetButton = document.querySelector("#reset-button");
const speakButton = document.querySelector("#speak-button");
const gateLabel = document.querySelector("#gate-label");
const translationAnnouncement = document.querySelector("#translation-announcement");
const siteLinksNav = document.querySelector("#site-links");
const sensorRows = [...document.querySelectorAll(".sensor-row")];
const scenarioButtons = [...document.querySelectorAll(".scenario-button")];
const journeySteps = [...document.querySelectorAll(".journey li")];
const scenarioIds = Object.keys(scenarios);
let activeScenario = "arrival";
let timers = [];
let isSpeaking = false;
const motionTiming = { translating: 1200, speaking: 2900 };
const waveformShape = [0.34, 0.62, 0.82, 0.48, 0.95, 0.68, 0.42, 0.88, 0.58, 1, 0.73, 0.38];

function clearTimers() { timers.forEach((timer) => window.clearTimeout(timer)); timers = []; }
function later(callback, delay) { const timer = window.setTimeout(callback, delay); timers.push(timer); }
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
  const order = ["sensors", "meaning", "feeling"];
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
  document.querySelector("#host-quote").textContent = `“${scenario.quote}”`;
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
// The hosts, not the browser. These are real ElevenLabs renders of Marco and
// Giulia in the station's own configured voices, so the demo sounds like the
// product instead of teaching every visitor that it is a speech engine.
// Relative paths keep the page working on a GitHub Pages project subpath.
const hostAudio = new Audio();
hostAudio.preload = "none";
function audioSrcFor(scenarioId) { return `public/audio/${scenarioId}.mp3`; }
function stopSpeech() {
  hostAudio.pause();
  isSpeaking = false;
  speakButton.classList.remove("is-speaking");
  speakButton.innerHTML = '<span aria-hidden="true">▶</span> Hear it on air';
}
hostAudio.addEventListener("ended", stopSpeech);
hostAudio.addEventListener("error", () => {
  // A missing or unplayable clip must not leave a dead button behind.
  stopSpeech();
  speakButton.innerHTML = '<span aria-hidden="true">▶</span> Clip unavailable — try again';
});
function primeAudio(scenarioId) {
  // Start fetching the clip while the translation animation runs, so the voice
  // begins the instant it is asked for rather than after a download.
  const src = audioSrcFor(scenarioId);
  if (hostAudio.src.endsWith(src)) return;
  hostAudio.src = src;
  hostAudio.load();
}
function startSpeaking() {
  // Reflect playback in the control without duplicating the click handler.
  isSpeaking = true;
  speakButton.classList.add("is-speaking");
  speakButton.innerHTML = '<span aria-hidden="true">■</span> Stop';
}
function playFromGesture() {
  // The voice IS the product; everything above it is setup. Waiting for a
  // second click meant a visitor could read the whole page and never hear the
  // one thing that is not copyable. This call sits inside the button's own
  // click handler, so it counts as a user gesture and autoplay rules allow it.
  // The manual control stays exactly where it was: if a browser refuses, the
  // page falls back to the old two-click path rather than to silence.
  hostAudio.currentTime = 0;
  const played = hostAudio.play();
  if (played && typeof played.catch === "function") {
    played.then(startSpeaking).catch(() => stopSpeech());
  } else {
    startSpeaking();
  }
}
function revealResult() {
  // On a narrow viewport the two panels stack, which puts the answer roughly two
  // screens below the button that asks for it: you tap the hero button
  // and nothing you can see changes. Bring the result to the reader when it is
  // off-screen, and only then, so the desktop side-by-side layout is untouched.
  const panel = document.querySelector(".meaning-panel");
  if (!panel) return;
  const box = panel.getBoundingClientRect();
  if (box.top >= 0 && box.top < window.innerHeight * 0.75) return;
  const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  panel.scrollIntoView({ behavior: reduced ? "auto" : "smooth", block: "start" });
}
function runTranslation({ fromGesture = false } = {}) {
  clearTimers(); stopSpeech();
  primeAudio(activeScenario);
  if (fromGesture) playFromGesture();
  revealResult();
  body.dataset.phase = "gathering";
  translateButton.disabled = true;
  translateLabel.textContent = "Listening to the house…";
  gateLabel.textContent = "Gathering signals";
  translationAnnouncement.textContent = "Listening to the house.";
  resetButton.hidden = false;
  updateJourney("sensors");
  later(() => {
    body.dataset.phase = "translating";
    translateLabel.textContent = "Finding the moment…";
    gateLabel.textContent = "Connecting clues";
    translationAnnouncement.textContent = "Connecting the sensor signals.";
    updateJourney("meaning");
  }, motionTiming.translating);
  later(() => {
    body.dataset.phase = "speaking";
    translateButton.disabled = false;
    translateLabel.textContent = "Hear another one";
    gateLabel.textContent = "On air";
    const scenario = scenarios[activeScenario];
    translationAnnouncement.textContent = `${scenario.heading} ${scenario.summary} On air: ${scenario.quote}`;
    updateJourney("feeling");
  }, motionTiming.speaking);
}
function resetExperience() {
  clearTimers(); stopSpeech(); body.dataset.phase = "idle"; translateButton.disabled = false;
  translateLabel.textContent = "Hear a moment"; gateLabel.textContent = "Ready to listen"; translationAnnouncement.textContent = ""; resetButton.hidden = true; updateJourney("sensors");
}
translateButton.addEventListener("click", () => {
  if (body.dataset.phase === "speaking") { const currentIndex = scenarioIds.indexOf(activeScenario); paintScenario(scenarioIds[(currentIndex + 1) % scenarioIds.length]); }
  runTranslation({ fromGesture: true });
});
resetButton.addEventListener("click", resetExperience);
scenarioButtons.forEach((button) => {
  button.setAttribute("aria-pressed", String(button.classList.contains("is-active")));
  // Picking a different moment mid-demo is a gesture too, so it plays as well.
  button.addEventListener("click", () => { const id = button.dataset.scenarioId; if (!id || id === activeScenario) return; paintScenario(id); if (body.dataset.phase !== "idle") runTranslation({ fromGesture: true }); });
});
speakButton.addEventListener("click", () => {
  if (isSpeaking) { stopSpeech(); return; }
  const src = audioSrcFor(activeScenario);
  // Reassign only on a real change so replaying the same moment does not refetch.
  if (!hostAudio.src.endsWith(src)) hostAudio.src = src;
  hostAudio.currentTime = 0;
  isSpeaking = true;
  speakButton.classList.add("is-speaking");
  speakButton.innerHTML = '<span aria-hidden="true">■</span> Stop';
  const played = hostAudio.play();
  if (played && typeof played.catch === "function") played.catch(() => stopSpeech());
});
initWaveforms();
paintScenario(activeScenario);
renderSiteLinks();
