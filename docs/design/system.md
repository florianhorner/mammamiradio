# Design System — Mamma Mi Radio

## Product Context
- **What this is:** AI-powered Italian radio station engine with live MP3 streaming, Claude-written banter, and AI-generated ads
- **Who it's for:** Listeners (guests who tap a link) and operators (the person running the station from /admin)
- **Space/industry:** Internet radio, AI audio, Home Assistant add-ons
- **Project type:** Web app (listener player + admin dashboard)

## Aesthetic Direction
- **Direction:** Volare Refined — Italian warmth on a dark canvas
- **Decoration level:** Intentional — film grain texture, single amber glow source, golden accent borders on featured cards
- **Mood:** The sunset lives in the accents, not the background. Warm espresso-dark foundation with golden highlights and cream text. Not NTS cold-black, not a SaaS dashboard. A dimly lit Italian bar where the radio is already playing.
- **Reference sites:** NTS Radio (category standard for dark radio UI), SomaFM (functional dark), Radio Garden (immersive interaction)
- **Visual thesis:** Midnight espresso with golden light — the Volare sunset preserved in typography and accent color on a unified dark stage

## Typography
- **Display/Hero:** Playfair Display 700 italic — the soul of the brand. Station name, show titles, track names, any display text that communicates identity or music.
- **Body:** Outfit 400–600 — replaces Inter. Warmer geometric sans with more personality. UI controls, metadata, labels, body text.
- **UI/Labels:** Outfit 700 uppercase, 0.18em tracking — eyebrow labels, section headers
- **Data/Tables:** JetBrains Mono 400–500 — timestamps, bitrates, diagnostics, queue positions, operator-only technical readouts. Supports tabular-nums natively.
- **Code:** JetBrains Mono
- **Loading:**
  ```html
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Playfair+Display:ital,wght@0,400;0,600;0,700;1,400;1,700&family=Outfit:wght@300;400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
  ```
- **Scale:**
  - Display: 36–52px (Playfair italic)
  - Title: 20–28px (Playfair)
  - Body: 14–15px (Outfit)
  - UI: 12–13px (Outfit 600)
  - Eyebrow: 9–10px (Outfit 700 uppercase, 0.18em tracking)
  - Mono: 11–13px (JetBrains Mono)
- **CSS custom properties:**
  ```css
  --font-display: 'Playfair Display', Georgia, serif;
  --font-body: 'Outfit', system-ui, -apple-system, sans-serif;
  --font-mono: 'JetBrains Mono', monospace;
  ```
- **Rule:** Playfair italic is the soul of the brand. Use it for any display text that communicates identity or music. Use Outfit for everything functional. Use JetBrains Mono for anything an engineer would read (bitrate, uptime, queue depth).

## Color

### Approach
Restrained — golden accent + warm neutrals. Color is rare and meaningful. The espresso-dark foundation makes every accent pop.

### CSS custom properties (`:root`)
```css
:root {
  /* Espresso foundation — warm dark, not cold charcoal */
  --bg:            #14110F;   /* page background */
  --bg-elevated:   #1C1714;   /* raised surfaces, ticker bg */
  --surface:       #54453A;   /* card backgrounds — ~2.07:1 vs --bg, schedule + hero stage */
  --surface-hover: #5E4D40;   /* interactive hover */
  --surface-strong:#6E5B49;   /* emphasized — ~2.96:1 vs --bg, about-card, dedica, ghost button */

  /* Text hierarchy */
  --cream:         #F5EDD8;   /* primary text */
  --cream-dk:      #EADDC4;   /* secondary text */
  --text-secondary:#CBB9A6;   /* metadata, artists */
  --muted:         #9B8574;   /* tertiary, labels, timestamps */

  /* Brand accents — the sunset lives here */
  --sun:           #F4D048;   /* brightest gold — live dot, play button */
  --sun2:          #ECCC30;   /* primary accent — buttons, active borders, eyebrow labels */
  --accent-warm:   #E8A030;   /* warm amber — secondary accent */

  /* Lancia red — interactive/dial/danger */
  --lancia:        #A02018;
  --lancia2:       #B82C20;

  /* Semantic (never change these) */
  --ok:            #2563EB;   /* success / connected / playing — BLUE, never green (colorblind) */
  --error:         #c44a4a;
  --warning:       #D97706;
  --news:          #e07038;   /* news_flash segment type — warm orange, distinct from warning amber */

  /* Structural */
  --line:          rgba(245,237,216,0.10);  /* admin dividers, panel borders — kept subtle */
  --line-strong:   rgba(245,237,216,0.32);  /* listener cards, hero stage, ghost button — visible at rest */
  --shadow:        rgba(0,0,0,0.35);
}
```

### Background gradient
```css
html {
  background:
    url("data:image/svg+xml,...grain...") repeat,
    linear-gradient(180deg, #1E1610 0%, #14110F 12%, #14110F 100%);
}
```
A subtle warm-to-dark gradient at the very top — the last trace of sunset at the horizon. The grain texture (0.04 opacity) adds material feel without noise.

### Sun glow
```css
body::before {
  content: '';
  position: fixed; top: -120px; right: 20%; z-index: 0;
  width: 400px; height: 400px; border-radius: 50%;
  background: radial-gradient(circle,
    rgba(244,208,72,0.10) 0%,
    rgba(236,204,48,0.03) 40%,
    transparent 70%
  );
  pointer-events: none;
}
```
Single light source, upper-right. Dimmer than the old Volare glow. Do NOT add multiple glow blobs.

### Dark mode
This IS dark mode. There is no light mode. Both listener and admin pages use this palette.

## Spacing
- **Base unit:** 4px
- **Density:** Comfortable (listener), Compact (admin)
- **Scale:** 2xs(2) xs(4) sm(8) md(16) lg(24) xl(32) 2xl(48) 3xl(64)

## Layout
- **Approach:** Grid-disciplined — single column (listener), multi-column (admin)
- **Grid:** Listener: single column, 640px max. Admin: 2-3 column grid, same max-width
- **Max content width:** 640px (listener), 960px (admin)
- **Border radius:** sm:4px, md:6px, lg:8px, xl:12px, full:9999px (badges, play button)
- **Unified shell:** Listener and admin share the same chrome, palette, and component library. The difference is information density, not visual language. Walking from listener to admin should feel like walking from the lounge to the control room.

## Motion

### Waveform — canonical, two variants, four states

Golden bars bouncing independently. Each bar has randomized target height, duration, and delay. The lack of sync is what makes it organic. **One component, two variants, four states. Used in all surfaces.** (Before consolidation there were four divergent waveforms across listener, dashboard, admin sidebar, and admin now-playing. This spec replaces all of them.)

**Variants:**
- `.waveform.hero` — large and dramatic. Used for the "tuning in / waiting for a listener" moment. Rounded pill bars (`border-radius: 999px`), 4px wide, 56px tall, breathes with height + opacity together.
- `.waveform.strip` — compact and unobtrusive. Used in now-playing strips during playback. Rectangular bars (`border-radius: 2px`), 3px wide, 24px tall.

**States (driven by classes / attrs):**
- **tuning / default** — active animation. Hero variant uses this on page load before first audio event.
- **playing** — same animation, strip variant swap. JS switches variant when `firstDataReceived` fires.
- **`.paused`** — `animation-play-state: paused`. Frozen mid-pose. Triggered by `togglePlay()` / `pause`.
- **`body[data-stopped="true"]`** — frozen AND faded to 40% opacity. Triggered by `session_stopped`; quiets the whole page while the operator has paused the station.

**CSS (lives in `static/base.css`):**

```css
.waveform {
  display: flex;
  align-items: flex-end;
  transition: opacity 0.3s ease;
}
.waveform-bar {
  background: var(--sun);
  animation: waveform-pulse var(--d, 0.8s) ease-in-out infinite alternate;
  animation-delay: var(--dl, 0s);
}

.waveform.hero {
  gap: 6px;
  height: 56px;
}
.waveform.hero .waveform-bar {
  width: 4px;
  border-radius: 999px;
  opacity: 0.42;
}

.waveform.strip {
  gap: 3px;
  height: 24px;
  align-items: center;
}
.waveform.strip .waveform-bar {
  width: 3px;
  border-radius: 2px;
  opacity: 0.45;
}

.waveform.paused .waveform-bar,
body[data-stopped="true"] .waveform .waveform-bar {
  animation-play-state: paused;
}
body[data-stopped="true"] .waveform { opacity: 0.4; }

@keyframes waveform-pulse {
  from { height: 4px;  opacity: 0.35; }
  to   { height: var(--h, 24px); opacity: 0.92; }
}
```

**JS (lives in `static/waveform.js`) — initializer + state toggle:**

```js
// Usage:
//   <div class="waveform" data-variant="hero"></div>
//   initWaveform(el)  // fills with 24 or 36 bars depending on variant
function initWaveform(el) {
  const variant = el.dataset.variant === 'hero' ? 'hero' : 'strip';
  el.classList.add('waveform', variant);
  const barCount = variant === 'hero' ? 24 : 36;
  const maxHeight = variant === 'hero' ? 56 : 22;
  for (let i = 0; i < barCount; i++) {
    const bar = document.createElement('div');
    bar.className = 'waveform-bar';
    const h = 6 + Math.random() * (maxHeight - 6);
    const d = (0.45 + Math.random() * 0.85).toFixed(2);
    const dl = (Math.random() * 0.7).toFixed(2);
    bar.style.setProperty('--h', h + 'px');
    bar.style.setProperty('--d', d + 's');
    bar.style.setProperty('--dl', dl + 's');
    el.appendChild(bar);
  }
}

function setWaveformPaused(el, paused) {
  el.classList.toggle('paused', paused);
}

function setWaveformVariant(el, variant) {
  el.classList.remove('hero', 'strip');
  el.classList.add(variant);
  el.dataset.variant = variant;
  // Re-init for correct bar count + height
  el.innerHTML = '';
  initWaveform(el);
}

// Auto-init on DOMContentLoaded
document.addEventListener('DOMContentLoaded', () => {
  document.querySelectorAll('.waveform:empty').forEach(initWaveform);
});
```

**Per-surface use:**
- Listener hero (`/`, first-load tuning-in): `<div class="waveform" data-variant="hero"></div>`
- Listener now-playing strip (after first audio): swap to `strip` via `setWaveformVariant`
- Admin sidebar "waiting for a listener" mini-slot: `data-variant="hero"` (dramatic but small surface — relies on height cap via sidebar container)
- Admin now-playing strip: `data-variant="strip"`

**Migration note:** deletes `.launch-waveform`, `.equalizer`, `.sw-bar` / `.sidebar-waveform`, `.pw-bar`, and the legacy `.wb` class. All per-surface waveforms collapse into this one component.

### Ticker scroll
32 seconds for full loop. Slow, ambient, hypnotic. Playfair Display italic text.
```css
.ticker { animation: ticker-scroll 32s linear infinite; }
```
Mask-image fade at 6% both edges for soft appearance/disappearance.

### Dial seeking
Organic momentum-based drift with overshoot-and-settle locking. Keep the wobble.

### Transitions
- Easing: enter(ease-out) exit(ease-in) move(ease-in-out)
- Duration: micro(50-100ms) short(150ms) medium(250ms) long(400ms)
- Track title change: no animation — Playfair at 22px has enough visual presence.

## Components

### Cards
Every card surface is **opaque** `--surface` (#54453A). Never semi-transparent.
```css
.card {
  background: var(--surface);
  border: 1px solid var(--line);
  border-radius: 8px;
  padding: 20px;
}
```

**Primary card** (now-playing) gets a golden top accent:
```css
.card.primary {
  border-top: 2px solid rgba(236,204,48,0.4);
}
.card.primary::before {
  content: ''; position: absolute; top: -1px; left: 24px; right: 24px; height: 2px;
  background: linear-gradient(90deg, transparent, rgba(244,208,72,0.6), transparent);
}
.card.primary::after {
  content: ''; position: absolute; top: 0; left: 0; bottom: 0; width: 3px;
  border-radius: 8px 0 0 8px;
  background: linear-gradient(180deg, var(--sun2) 0%, rgba(236,204,48,0.1) 100%);
}
```

### Connect CTA (cream contrast card)
The **only** light-background element. Breaks the dark card pattern deliberately for visual hierarchy.
```css
.connect-cta {
  background: var(--cream);
  border-left: 4px solid var(--sun);
  border-radius: 10px;
  padding: 16px 20px;
}
```
- Headline: Playfair Display 17px bold italic, dark text
- Subtitle: Outfit 11px, `rgba(20,17,15,0.55)`
- Hidden when station is fully configured

### Buttons
Primary (golden): `background: var(--sun2); color: var(--bg); box-shadow: 0 2px 12px rgba(236,204,48,0.3)`
Ghost: `background: rgba(245,237,216,0.08); border: 1px solid rgba(245,237,216,0.2); color: rgba(245,237,216,0.7)`
Never red buttons for primary actions.

### Play button
Golden with three-layer glow. Blue when playing.
```css
.play-btn {
  width: 48px; height: 48px; border-radius: 50%;
  background: var(--sun2); color: var(--bg);
  box-shadow: 0 4px 20px rgba(236,204,48,0.4),
              0 0 0 6px rgba(236,204,48,0.08),
              0 0 28px rgba(236,204,48,0.15);
}
.play-btn.playing {
  background: var(--ok); color: #fff;
  box-shadow: 0 4px 20px rgba(37,99,235,0.4), 0 0 0 6px rgba(37,99,235,0.08);
}
```

### Status system — canonical, 5 states × 3 visual forms

**One status system. Five states. Three visual forms.** Every state declaration pairs color with a unique SHAPE — color alone is never sufficient. Red-green colorblindness rules: `--ok` is blue, never green; `--warning` is amber, not orange; `--error` is red with ✗ shape.

Lives in `static/base.css`. Replaces the ad-hoc Engine Room green-button-vs-blue-check inconsistency, the admin sidebar `.pipeline-dot` class family, and any per-surface status chips.

**The 5 states:**

| State | Color | Shape | Meaning |
|-------|-------|-------|---------|
| `ready` | `--ok` (blue #2563EB) | ✓ | Active and healthy. Default "working correctly." |
| `working` | `--sun` (gold) | pulsing ● | Transient busy state (banter generating, track downloading). Not a long-term display. |
| `degraded` | `--warning` (amber) | △ | **Operator-honesty case (v2.10.6).** Functioning, but on fallback. "Anthropic key configured, auth suspended, OpenAI taking calls." |
| `blocked` | `--error` (red) | ✗ | Failed. Not functioning. Needs attention. |
| `idle` | `--muted` (gray) | ○ | Not running, not failed. Waiting or off. |

**Rationale for the 5:** Before this spec, the project's status UI conflated "ready" and "degraded" (green button showing ready while fallback was active was the operator-dishonest case that Florian flagged twice). A "degraded" state distinct from "ready" makes the operator-honesty principle mechanically visible: if the UI says ✓, the thing is actually ready. If it's on fallback, you see △.

**The 3 visual forms (same states, different density):**

1. **`.status-chip`** — full chip with label. Used in Engine Room rows, admin card headers. ~10-12px label, rounded pill, bordered.
2. **`.status-dot`** — compact pill with icon + short label. Used in admin sidebar pipeline status, header chips. ~9-10px label.
3. **`.status-inline`** — icon only, no pill. Used inline next to text (queue-source tags, log entries).

**CSS:**

```css
/* Base (shared across all three forms) */
.status-chip, .status-dot {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-weight: 600;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  padding: 4px 10px;
  border-radius: 20px;
  border: 1px solid currentColor;
  background: color-mix(in srgb, currentColor 8%, transparent);
}
.status-chip { font-size: 10px; }
.status-dot  { font-size: 9px; padding: 3px 8px; }

.status-chip::before {
  font-weight: 700;
  font-size: 0.95em;
  line-height: 1;
}
.status-dot > .dot {
  width: 6px; height: 6px; border-radius: 50%;
  background: currentColor;
  display: inline-block;
}
.status-inline {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  font-size: 0.9em;
}
.status-inline::before { font-weight: 700; }

/* State: ready */
.status-chip.ready,
.status-dot.ready,
.status-inline.ready       { color: var(--ok); }
.status-chip.ready::before,
.status-inline.ready::before { content: "\2713"; }       /* ✓ */

/* State: working */
.status-chip.working,
.status-dot.working,
.status-inline.working     { color: var(--sun); }
.status-chip.working::before,
.status-inline.working::before { content: "\25CF"; animation: status-pulse 1.4s ease-in-out infinite; } /* ● */
.status-dot.working > .dot   { animation: status-pulse 1.4s ease-in-out infinite; }

/* State: degraded (operator-honesty fallback) */
.status-chip.degraded,
.status-dot.degraded,
.status-inline.degraded    { color: var(--warning); }
.status-chip.degraded::before,
.status-inline.degraded::before { content: "\25B3"; }    /* △ */
.status-dot.degraded > .dot { border-radius: 0; width: 0; height: 0;
  border-left: 4px solid transparent; border-right: 4px solid transparent;
  border-bottom: 7px solid currentColor; background: transparent; }

/* State: blocked */
.status-chip.blocked,
.status-dot.blocked,
.status-inline.blocked     { color: var(--error); }
.status-chip.blocked::before,
.status-inline.blocked::before { content: "\2717"; }     /* ✗ */

/* State: idle */
.status-chip.idle,
.status-dot.idle,
.status-inline.idle        { color: var(--muted); }
.status-chip.idle::before,
.status-inline.idle::before { content: "\25CB"; }        /* ○ */

@keyframes status-pulse {
  0%, 100% { opacity: 0.4; }
  50%      { opacity: 1; }
}
```

**Usage examples:**

```html
<!-- Engine Room row -->
<span class="status-chip ready">AI Writing</span>
<span class="status-chip ready">Music Sources</span>
<span class="status-chip degraded">AI Fallback</span>
<span class="status-chip blocked">HA Disconnected</span>

<!-- Admin sidebar pipeline -->
<span class="status-dot ready"><span class="dot"></span>AI</span>
<span class="status-dot degraded"><span class="dot"></span>AI Fallback</span>
<span class="status-dot idle"><span class="dot"></span>HA</span>

<!-- Inline (queue log) -->
<li>Banter generated <span class="status-inline ready">ok</span></li>
<li>Ad generation <span class="status-inline working">running</span></li>
```

**Accessibility:**
- Every status element MUST carry `aria-label="status: <state>"` for screen readers — color+shape is for sighted users, aria-label is for the rest.
- `.status-chip.working` animation respects `prefers-reduced-motion`; pulse disables, color+shape communicates the state.

**Migration map — what gets replaced when we implement:**

| Current | → | New |
|---------|---|-----|
| Engine Room "AI writing ready" green button | → | `.status-chip.ready AI Writing` |
| Engine Room "Music Sources ready" blue+check | → | `.status-chip.ready Music Sources` |
| Admin `.pipeline-dot .dot.ok` (current: `#2563EB`) | → | `.status-dot.ready` |
| Admin `.pipeline-dot .dot.warn` with `▲` | → | `.status-dot.degraded` |
| Admin `.pipeline-dot .tri` + "No AI" | → | `.status-dot.blocked No AI` or `.status-dot.idle AI Off` |
| `.badge-ok / .badge-live / .badge-warn / .badge-err` (legacy) | → | `.status-chip.ready / .working / .degraded / .blocked` |
| Ad-hoc `<span class="dot ok">` colored circles | → | `.status-inline.<state>` |

The legacy `.badge-*` classes stay temporarily as aliases to the new `.status-chip.<state>` during migration. After migration completes, delete the `.badge-*` classes.

### Inputs
Dark interior, cream text, golden focus ring.
```css
.input {
  background: rgba(20,17,15,0.6);
  border: 1px solid var(--line-strong);
  color: var(--cream);
}
.input:focus { border-color: rgba(236,204,48,0.5); }
```

### Eyebrow labels
```css
.eyebrow {
  font-family: var(--font-body);
  font-size: 9px; font-weight: 700;
  letter-spacing: 0.18em; text-transform: uppercase;
  color: var(--sun2);   /* golden for music sections */
  /* or: color: var(--muted);  for structural labels */
}
```

## Listener site composition — canonical

The listener surface (served at `/` and `/listen`) is a radio station website, not a player widget. Five-band vertical composition, designed for desktop first (1440px), scales down gracefully. Reference wireframe: `.context/designs/unified-player-20260421/site-v1.html` (approved 2026-04-21).

**Ordering is normative. Each band can be edited independently, but the order and presence of all five cannot.**

### 1. Tricolor + Nav
- 3px tricolor band flush to page top (green `#009246` / cream `rgba(255,255,255,0.9)` / red `#CE2B37`, equal thirds).
- Sticky nav below: `backdrop-filter: blur(12px)` on `rgba(20,17,15,0.85)` background, 1px bottom border in `--line`.
- Layout: `[logo + station name] [center nav links] [primary CTA pill]`.
- Logo: 38px gold gradient circle with italic "M" + Lancia-red "i", paired with `<h1>Mamma <span class="mi">Mi</span> Radio</h1>` in Playfair italic 24pt.
- Nav links: "In Onda / Palinsesto / Dediche / About" in Outfit 13pt, active state `--sun`.
- CTA: golden pill "Ascolta Ora" with chevron. Distinct from the small play button in the now-playing strip — this is the invitation to restart, that is playback control.

### 2. Now-playing strip (persistent band below nav)
- Layout: `[ON AIR chip] [track · artist] [progress bar with time codes] [prev/play/next controls]`.
- ON AIR chip: Lancia red background, cream dot, uppercase letter-spaced "On Air" label.
- Track in Playfair italic 16pt, artist in Outfit 13pt muted.
- Progress bar: 3px track in `rgba(245,237,216,0.12)`, fill in `--sun`, time codes in JetBrains Mono 11pt.
- Play button: 36px gold pill matching `.play-btn` spec.
- Persistent — same strip, same state, same position on every listener page.

### 3. Hero (2-column grid)
- Grid: `1.1fr 1fr`, min-height 540px. No padding between columns; the two sides meet edge-to-edge.
- **Left column** (copy): padding `72px 32px 72px 64px`. Vertical stack:
  1. Eyebrow kicker "In Diretta · 96,7 FM · Milano" — Outfit 11pt uppercase letter-spaced `0.28em`, gold, with a 32px gold rule prefix.
  2. Headline in Playfair Display italic, `font-size: 72px; line-height: 0.98`. Color-accented word pattern: `<span class="accent">italiana</span>` in `--sun`. The accent-word doubles as a brand signature parallel to Gold-Mi.
  3. Subcopy in Playfair italic 17pt, 48ch max-width, `rgba(245,237,216,0.65)`.
  4. Action pair: primary golden pill "Ascolta Ora" + ghost border "Il Palinsesto".
  5. Stats row (3 cells): Playfair italic 28pt number + Outfit 10pt uppercase letter-spaced label. Separated from above by a `--line` top border with 24px padding.
- **Right column** (image + widget): aspect-filled hero image. Until AI scene is available, sepia-warm gradient placeholder. Edge-gradient mask to `--bg` on both sides so the image reads as inset, not edge-to-edge.
- **Dial widget overlay**: absolutely positioned bottom-right, 28px inset from corner, 340px wide. Compact version of the FM tuning dial — frequency readout, signal bars, band with needle locked at 96.7. On first-page-load does the tuning-in animation; after signal-lock settles to static ambient state. Radio metaphor preserved without dominating.

### 4. Palinsesto
- Section head: "Stasera in Onda" in Playfair italic 34pt + right-aligned dateline in Outfit 12pt uppercase.
- Bottom border on section head in `--line` with 18px padding.
- Grid: `1.2fr 1fr 1fr 1fr` — 4 slot cards, first cell wider to emphasize the NOW slot.
- Each slot card:
  - Background `--surface`, border `--line`, `border-top: 1px solid rgba(245,237,216,0.14)`, 12px rounded.
  - `.slot.now` variant: `border-top: 2px solid var(--sun2)`, subtle gold wash gradient top-to-surface, gold glow box-shadow.
  - Content vertical stack:
    1. Time range + optional `<span class="live">` badge (Lancia background when NOW).
    2. Kind label — 9pt uppercase letter-spaced: `Musica` / `Banter` / `Sponsored` / `News` / `Jingle`.
    3. Title — Playfair italic 22pt.
    4. Host/byline — Outfit 12pt `--text-secondary`, with `·` dot separator between entries.
- Source of truth: the producer queue (`/status` upcoming array). Real data from the stream, rendered in the role-play frame.

### 5. Dediche & Saluti
- Section head: "Dediche & Saluti" Playfair italic 34pt + right-aligned "Dai nostri ascoltatori" Outfit 12pt uppercase.
- Grid: `1.6fr 1fr` — quote stack on left, form sidecar on right.
- **Pull-quote card** (stack):
  - Background `--surface`, 14px rounded, 32x36 padding, `--line` border.
  - Large Playfair italic quote glyph `"` absolutely positioned top-left, 84pt gold at 30% opacity.
  - Quote text: Playfair italic 20pt, tight leading 1.5.
  - First letter dropcap: Playfair 900 italic 68pt, Lancia red, float-left with margin adjustment.
  - Meta line (bottom, above a `--line` top border): `<strong>Name</strong>` (gold uppercase letter-spaced) · city · "letta in onda alle HH:MM" · right-aligned date in JetBrains Mono.
- **Form sidecar**:
  - Same surface treatment as pull-quote but with `border-top: 2px solid var(--sun2)` accent.
  - Stack: eyebrow "Manda un Saluto" + Playfair italic title "La tua voce, in diretta." + Outfit 14pt description + name input + message textarea + golden "Manda al DJ ▶" submit.
- Closes the loop: listener submits a message → host reads it on-air → it appears in this section with the "letta in onda alle HH:MM" annotation. This is the operator-honesty payoff on the listener side (what you said got heard, and here is the time).

### 6. Ticker + 7. Footer
- Ticker: already specified in Motion section. Italian local color scroll.
- Footer: 28px padding 64px horizontal, three cells: left "Mamma Mi Radio · 96,7 FM Milano · dal 1987" · center mini tricolor 90px wide 3px tall · right "Produced by AI · written in Italia". Outfit 12pt, 40% opacity cream.

### Accent-word pattern (reusable)

The Gold-Mi rule extends. Any Playfair italic heading of 28pt+ may use a single accent-word in `--sun` for brand signature. Examples: `La notte è <span class="accent">italiana</span>`, `L'anima <span class="accent">della vera Italia</span>`. One accent-word per heading, always the emotional anchor of the phrase, always `--sun` (or `--lancia` if the phrase warrants physical-instrument energy — rare, use sparingly). Never more than one accent word per heading.

## Rules and anti-patterns

| Rule | Reason |
|------|--------|
| Never semi-transparent card backgrounds | Warm bg bleeds through, causes blur |
| Never multiple radial glow blobs on body | Same blur effect — use single directional glow |
| Never green for success / connected / playing | Red-green colorblind. Use `--ok` (#2563EB) |
| Never cold grey or charcoal as the dominant background | The warmth of #14110F is the Italian identity |
| Never Inter/Roboto/Arial for body text | Use Outfit. Inter is the old system. |
| Never Outfit for the station name | Always Playfair Display italic |
| Never light backgrounds on cards (except Connect CTA) | The cream CTA is the sole exception, for hierarchy |
| Never a full card for troubleshooting/error states | Demote to `(?)` toggle — error UI must never compete with upgrade prompts |
| Always pair semantic color with a shape icon | `.status-ok::before { content: "✓" }` — not color alone |
| Never a separate palette for listener vs admin | One palette, two densities. The system is unified. |

## Colorblind accessibility

This design system is built for **red-green colorblind accessibility**. This affects all UI work:

- Success / connected / active: `#2563EB` (blue), never green
- Error: `#c44a4a` (red — only for errors, not active states)
- Warning: `#D97706` (amber)
- Teal is also hard to read — avoid

All semantic states must pair a color with a **shape**: checkmark (✓), triangle (△), cross (✗), or circle (○). Color alone is never sufficient for status communication.

## File locations

| Asset | Path |
|-------|------|
| Admin | `mammamiradio/web/templates/admin.html` |
| Listener | `mammamiradio/web/templates/listener.html` |
| Logo SVG | `mammamiradio/assets/logo.svg` |
| HA add-on icon | `ha-addon/mammamiradio/icon.png` (256px) |
| HA logo | `ha-addon/mammamiradio/logo.png` (512px) |
| Design preview | `/tmp/design-preview-volare-refined.html` (local only, not committed) |

To regenerate HA add-on PNGs from SVG:

```bash
cairosvg mammamiradio/assets/logo.svg -o ha-addon/mammamiradio/icon.png -W 256 -H 256
cairosvg mammamiradio/assets/logo.svg -o ha-addon/mammamiradio/logo.png -W 512 -H 512
```


## Decisions Log
| Date | Decision | Rationale |
|------|----------|-----------|
| 2026-04-11 | Unified espresso-dark palette, replacing split Volare sunset + dark listener palettes | Listener and admin felt like two different products. Category standard (NTS, SomaFM) uses dark backgrounds. Italian warmth preserved via accents, typography, and surface materials. |
| 2026-04-11 | Kept Playfair Display italic for display | Brand equity from live tests — "Mamma Mi Radio" in Playfair italic IS the identity |
| 2026-04-11 | Replaced Inter with Outfit for body | Inter is overused and generic. Outfit is warmer, more geometric, with enough personality for an Italian radio station |
| 2026-04-11 | Added JetBrains Mono for technical readouts | Timestamps, bitrates, queue depth, diagnostics. Clear signal that this data is for the operator. |
| 2026-04-11 | Reviewed by Codex (GPT-5.4) and Claude subagent | Both independently converged on the same core direction: warm dark, serif display, unified palette. Codex proposed Fraunces, subagent proposed Cormorant Garamond. User chose to keep Playfair. |
| 2026-04-21 | Waveform spec formalized: one component, two variants (hero / strip), four states (tuning / playing / paused / stopped) | Before: four divergent waveform implementations across listener, dashboard, admin sidebar, admin now-playing. Consolidation dictated by `/design-consultation` during the three-surfaces-to-two migration. |
| 2026-04-21 | Status system formalized: 5 states × 3 visual forms with color + shape pair per state | Engine Room "AI ready = green button" while Anthropic auth was suspended was the operator-dishonest lie flagged in live tests. `degraded` state (amber △) now exists as a first-class state. |
| 2026-04-21 | Listener surface re-scoped from "player widget" to "radio station website" | Perplexity-generated Italian-radio mockups (Sole / Bella Italia / Notturno) made visible that "cohesive, not disjointed" means the full website: nav + persistent now-playing strip + hero with image + palinsesto + dediche. Florian approved `site-v1.html` direction. Role-play schedule ("Stasera in Onda") + magazine pull-quote Dediche pattern are net-new sections. |
| 2026-04-21 | Accent-word pattern extends Gold-Mi | One italic accent-word per Playfair heading, always in `--sun`, always the emotional anchor. Extends the brand signature from the station name to every major heading. Confirmed by Perplexity Bella Italia variant ("*della vera Italia*" pattern). |
