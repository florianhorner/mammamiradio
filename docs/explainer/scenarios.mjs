// The single source of truth for the four home moments.
//
// Everything that describes a scenario lives here and only here: the copy the
// page renders, the sensor rows, the segment script the clip producer reads,
// the transcript a screen reader gets, and the cue point the reveal fires on.
// index.html keeps one hardcoded copy of the arrival rows as the pre-JS first
// paint; app.js repaints from this file the moment it loads.
//
// Why a .mjs module and not scenarios.json: the browser cannot import JSON
// without import attributes (still uneven across engines), and a runtime
// fetch() would add a failure mode to a page whose whole job is never showing
// one. A data-only ES module imports identically in the browser (app.js),
// in Node (scripts/produce-segments.mjs, scripts/build.mjs), and in tests.
//
// Field notes:
// - reachability: "day-one" means every entity shown is available to a fresh
//   install in narrow ambient context (weather.ambient + sun.ambient only,
//   per mammamiradio/home/authorization.py). "home-grant" means the moment
//   needs the operator to let the hosts use household entities. The page must
//   always contain at least one "day-one" scenario — a build guard and a test
//   enforce it, so honesty cannot regress silently.
// - beats: the segment script for scripts/produce-segments.mjs. Ordered.
//   "tail" is a starter-catalog music tail (CC-BY, attribution required on
//   the page), "imaging" ids reference assets/imaging/manifest.json (CC0),
//   "voice" beats are rendered host speech.
// - revealAtSec: the playback offset where the home moment lands and the
//   sensor reveal fires. Measured by scripts/produce-segments.mjs --render
//   and copied from segments.manifest.json — humans do not guess it. The
//   build fails on a mismatch with the produced manifest.
// - transcript: what a visitor who cannot hear the clip reads. Until the
//   segments are produced this carries the host line only; the producer
//   replaces it with the full segment text.

const scenarios = {
  arrival: {
    id: "arrival",
    time: "18:42 · Tuesday",
    tag: "Welcome home",
    heading: "Someone’s home early.",
    summary: "The front door opened four minutes ago. Evening light is fading. The kitchen is still quiet.",
    host: "Marco noticed",
    quote: "Someone’s home early, the light is fading, and it’s fourteen degrees outside. Benvenuto a casa.",
    reachability: "home-grant",
    color: "#f4d048",
    rgb: "244, 208, 72",
    sensors: [
      ["⌂", "person.flo", "home · 4 min"],
      ["↗", "binary_sensor.front_door", "closed"],
      ["☼", "sensor.living_room_lux", "36 lx"],
      ["°", "sensor.outdoor_temperature", "14 °C"],
      ["♪", "media_player.kitchen", "idle"],
    ],
    beats: [
      { kind: "tail", note: "starter-catalog song winding down, ~3s" },
      { kind: "imaging", id: "identity.sweeper" },
      {
        kind: "voice", who: "marco+giulia", note: "mid-bit, unrelated to the house",
        lines: [
          { who: "marco", text: "No, no, no. You cannot put cream in carbonara. This is not an opinion, Giulia, it is the law." },
          { who: "giulia", text: "You read one cookbook, Marco. Half of it. The half with pictures." },
        ],
      },
      {
        kind: "voice", who: "marco", note: "the home moment — the quote", moment: true,
        lines: [{ who: "marco", text: "Someone’s home early, the light is fading, and it’s fourteen degrees outside. Benvenuto a casa." }],
      },
      { kind: "imaging", id: "bumper.ad-out" },
    ],
    revealAtSec: 17.94,
    transcript: "Marco: No, no, no. You cannot put cream in carbonara. This is not an opinion, Giulia, it is the law. Giulia: You read one cookbook, Marco. Half of it. The half with pictures. Marco: Someone’s home early, the light is fading, and it’s fourteen degrees outside. Benvenuto a casa.",
  },
  coffee: {
    id: "coffee",
    time: "14:07 · Tuesday",
    tag: "Classic Tuesday",
    heading: "The kitchen just woke up.",
    summary: "The coffee machine started. Someone arrived earlier than usual. The room is cooler than the rest of the house.",
    host: "Giulia connected the dots",
    quote: "The coffee machine just started, someone’s home early, and it’s fourteen degrees in here. Classic Tuesday.",
    reachability: "home-grant",
    color: "#e8a030",
    rgb: "232, 160, 48",
    sensors: [
      ["◉", "sensor.coffee_machine_power", "842 W"],
      ["⌂", "person.flo", "home · early"],
      ["°", "sensor.kitchen_temperature", "14 °C"],
      ["◌", "binary_sensor.kitchen_presence", "detected"],
      ["☼", "sensor.kitchen_lux", "118 lx"],
    ],
    beats: [
      { kind: "tail", note: "starter-catalog song winding down, ~3s" },
      { kind: "imaging", id: "identity.station-id" },
      {
        kind: "voice", who: "marco+giulia", note: "mid-bit, unrelated to the house",
        lines: [
          { who: "marco", text: "The two o’clock slot is the most important slot in radio. Ask anyone. Ask me." },
          { who: "giulia", text: "I asked the schedule. It says nobody is listening, Marco." },
        ],
      },
      {
        kind: "voice", who: "giulia", note: "the home moment — the quote", moment: true,
        lines: [{ who: "giulia", text: "The coffee machine just started, someone’s home early, and it’s fourteen degrees in here. Classic Tuesday." }],
      },
      { kind: "imaging", id: "bumper.ad-out" },
    ],
    revealAtSec: 17.53,
    transcript: "Marco: The two o’clock slot is the most important slot in radio. Ask anyone. Ask me. Giulia: I asked the schedule. It says nobody is listening, Marco. Giulia: The coffee machine just started, someone’s home early, and it’s fourteen degrees in here. Classic Tuesday.",
  },
  laundry: {
    id: "laundry",
    time: "20:16 · Thursday",
    tag: "Domestic breaking news",
    heading: "The laundry finished. Nobody noticed.",
    summary: "The cycle ended more than two hours ago. People are home, but nobody has entered the laundry room since.",
    host: "Marco has an update",
    quote: "Breaking news from the laundry room: it’s done. It’s been done for two hours. Nobody cares but us.",
    reachability: "home-grant",
    color: "#9cab7e",
    rgb: "156, 171, 126",
    sensors: [
      ["↻", "sensor.washing_machine", "finished · 2h 07m"],
      ["◌", "binary_sensor.laundry_motion", "clear · 2h 12m"],
      ["⌂", "zone.home", "2 people"],
      ["↗", "binary_sensor.laundry_door", "closed"],
      ["⚡", "sensor.washer_power", "0.4 W"],
    ],
    beats: [
      { kind: "tail", note: "starter-catalog song winding down, ~3s" },
      { kind: "imaging", id: "identity.sweeper" },
      {
        kind: "voice", who: "marco+giulia", note: "mid-bit, unrelated to the house",
        lines: [
          { who: "giulia", text: "Your sports report tonight was four minutes of you describing one penalty kick." },
          { who: "marco", text: "One perfect penalty kick. Cinema, Giulia. Pure cinema." },
        ],
      },
      {
        kind: "voice", who: "marco", note: "the home moment — the quote", moment: true,
        lines: [{ who: "marco", text: "Breaking news from the laundry room: it’s done. It’s been done for two hours. Nobody cares but us." }],
      },
      { kind: "imaging", id: "bumper.ad-out" },
    ],
    revealAtSec: 15.66,
    transcript: "Giulia: Your sports report tonight was four minutes of you describing one penalty kick. Marco: One perfect penalty kick. Cinema, Giulia. Pure cinema. Marco: Breaking news from the laundry room: it’s done. It’s been done for two hours. Nobody cares but us.",
  },
  quiet: {
    // The one moment a brand-new install can actually produce. Narrow ambient
    // context projects exactly two entities — the sun and the weather — and
    // this scenario uses nothing else. It exists so the page demonstrates at
    // least one thing a stranger can reach on day one, before any home grant.
    id: "quiet",
    time: "21:48 · Sunday",
    tag: "Day one, no permissions",
    heading: "Evening, officially.",
    summary: "The sun set twenty minutes ago. Clear night, eleven degrees and falling. This is everything a brand-new station knows about your home — and it is already a segment.",
    host: "Giulia checks the sky",
    quote: "Sunset was twenty minutes ago, eleven degrees and clear. È ufficialmente sera. Act accordingly.",
    reachability: "day-one",
    color: "#60a5fa",
    rgb: "96, 165, 250",
    sensors: [
      ["☾", "sun.sun", "below_horizon · 20 min"],
      ["☼", "weather.home", "clear-night"],
      ["°", "weather.home · temperature", "11 °C"],
      ["◌", "weather.home · humidity", "72 %"],
      ["↗", "sun.sun · next_dawn", "06:12"],
    ],
    beats: [
      { kind: "tail", note: "starter-catalog song winding down, ~3s" },
      { kind: "imaging", id: "identity.station-id" },
      {
        kind: "voice", who: "marco+giulia", note: "mid-bit, unrelated to the house",
        lines: [
          { who: "marco", text: "Sunday nights the phones go quiet. I respect it. Even my conspiracy hotline takes the evening off." },
          { who: "giulia", text: "It’s a landline, Marco, and you unplug it so your ego can charge." },
        ],
      },
      {
        kind: "voice", who: "giulia", note: "the home moment — the quote", moment: true,
        lines: [{ who: "giulia", text: "Sunset was twenty minutes ago, eleven degrees and clear. È ufficialmente sera. Act accordingly." }],
      },
      { kind: "imaging", id: "bumper.ad-out" },
    ],
    revealAtSec: 18.87,
    transcript: "Marco: Sunday nights the phones go quiet. I respect it. Even my conspiracy hotline takes the evening off. Giulia: It’s a landline, Marco, and you unplug it so your ego can charge. Giulia: Sunset was twenty minutes ago, eleven degrees and clear. È ufficialmente sera. Act accordingly.",
  },
};

export const scenarioIds = Object.keys(scenarios);
export default scenarios;
