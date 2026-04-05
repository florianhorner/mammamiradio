# MammaMiRadio Design System

Visual design reference for `dashboard.html`, `listener.html`, and any future UI surfaces.
The source of truth for colors, typography, component patterns, and motion.

---

## Concept: Volare

**Reference image:** `.context/attachments/image-v1.jpg` — the 1958 Domenico Modugno
*Volare* record cover. Color distribution: ~50% warm orange-red sunset sky, ~20% golden
sea, ~15% warm sienna road/buildings, ~10% deep Lancia red car, ~5% dark palm silhouettes.

**The idea:** You're not looking at software. You're looking at a sunset. The radio is
already playing. The station is alive before you do anything.

---

## Palette

### CSS custom properties (`:root`)

```css
/* Sky — the dominant background color */
--sky1:   #C44020;   /* deep horizon red */
--sky2:   #D45228;   /* mid sunset orange */
--sky3:   #E07038;   /* bright amber-orange */

/* Sun — golden accent, play button, active states */
--sun:    #F4D048;
--sun2:   #ECCC30;

/* Buildings in shadow — all card surfaces */
--sienna: #823218;   /* primary card bg */
--sienna2:#924020;   /* secondary card bg, dial band */
--sienna3:#A04A24;   /* hover state */

/* Road — mid-tone (use sparingly) */
--road:   #A86838;

/* Cream — all text */
--cream:  #F5EDD8;
--cream-dk: #EADDC4;   /* secondary / muted text */

/* Silhouette — deepest dark, use only for FM dial interior */
--shadow: #2A1008;

/* Lancia red — interactive / FM needle / error indicators */
--lancia: #A02018;
--lancia2:#B82C20;

/* Semantic (never change these) */
--ok:      #2563EB;   /* success / connected / playing — BLUE, never green (colorblind) */
--error:   #c44a4a;
--warning: #c4a04a;
--muted:   rgba(245,237,216,0.45);
```

### Named swatches

| Name | Hex | Role |
|------|-----|------|
| Horizon | `#C44020` | Background gradient start |
| Sunset | `#D45228` | Background gradient mid |
| Amber sky | `#E07038` | Background gradient peak |
| Golden sun | `#F4D048` | Play button, live dot, active accents |
| Sun glow | `#ECCC30` | Card top borders, dial status, eyebrow labels |
| Sienna | `#823218` | Card surfaces (now-playing, connect hero) |
| Sienna dark | `#924020` | Secondary cards, dial card |
| Sienna hover | `#A04A24` | Hover state for interactive cards |
| Cream | `#F5EDD8` | All body text, headings |
| Lancia | `#B82C20` | FM dial needle, interactive red, connect top border |
| Shadow | `#2A1008` | FM dial interior only |
| Signal blue | `#2563EB` | `--ok`: Spotify connected, play button active |

### Background gradient

```css
background: linear-gradient(175deg,
  #C44020 0%,
  #D45228 25%,
  #E07038 55%,
  #CC5A30 80%,
  #B04020 100%
);
```

A slight ~175deg tilt gives it warmth without being flat. Don't use radial gradients
on the body — they produce the "verschwommen" (blurry, unclear) effect.

### Grain texture

Apply over the background via `background-image` stack or `body::before`:

```css
url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='200' height='200'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.85' numOctaves='4' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='200' height='200' filter='url(%23n)' opacity='0.05'/%3E%3C/svg%3E") repeat
```

Opacity `0.05` (5%). Lower feels like a clean mockup. Higher feels like noise.

### Sun glow

```css
body::before {
  content: '';
  position: fixed; top: -80px; right: 25%; z-index: 0;
  width: 300px; height: 300px; border-radius: 50%;
  background: radial-gradient(circle,
    rgba(244,208,72,0.3) 0%,
    rgba(240,160,40,0.12) 40%,
    transparent 70%
  );
  pointer-events: none;
}
```

Single light source, upper-right. Do NOT add multiple glow blobs — causes blur.

---

## Typography

### Fonts

```html
<link href="https://fonts.googleapis.com/css2?family=Playfair+Display:ital,wght@0,400;0,600;0,700;1,400;1,700&family=Inter:wght@300;400;500;600;700&display=swap" rel="stylesheet">
```

| Role | Font | Weight | Style | Size |
|------|------|--------|-------|------|
| Station name | Playfair Display | 700 | **italic** | 36px |
| Station subtitle | Playfair Display | 400 | italic | 11px |
| Now playing track | Playfair Display | 700 | normal | 20px |
| Dial frequency | Playfair Display | 700 | normal | 22px |
| Connect card heading | Playfair Display | 700 | italic | 22px |
| Card headings (h3) | Inter | 600 | normal | 14px |
| Body / controls | Inter | 400–500 | normal | 13–15px |
| Eyebrow labels | Inter | 700 | normal | 9px, 0.18–0.2em tracking, uppercase |

**Rule:** Playfair italic is the soul of the brand. Use it for any display text that
communicates identity or music. Use Inter for everything functional.

### Station name treatment

```css
font-family: 'Playfair Display', Georgia, serif;
font-size: 36px; font-weight: 700; font-style: italic;
line-height: 0.92; letter-spacing: -0.02em;
color: var(--cream);
text-shadow: 0 2px 20px rgba(42,16,8,0.4);
```

Tight leading (`0.92`) makes it feel like a masthead. The shadow adds depth without glow.

---

## Components

### Body / page shell

```css
body {
  max-width: 640px; margin: 0 auto;
  padding: 0 16px 96px;
  color: var(--cream);
  /* background: gradient + grain (see above) */
}
```

Single column, 640px max. No sidebar. Cards stack vertically.

### Cards

Every card surface is **opaque sienna**, never semi-transparent. Transparency causes the
"verschwommen" (blurry/hazy) effect because the warm background bleeds through.

```css
.card {
  background: var(--sienna2);   /* #924020 */
  border: 1px solid rgba(245,237,216,0.08);
  border-top: 1px solid rgba(245,237,216,0.14);   /* subtle light edge */
  border-radius: 8px; padding: 18px; margin-bottom: 12px;
}
```

**Primary cards** (now-playing) use `--sienna` (`#823218`) — slightly darker.

**Exception — the Connect CTA card** uses an inverted cream palette (see [Connect CTA](#connect-cta)
below). This is the *only* card that breaks the sienna rule, deliberately, to create visual
hierarchy between the upgrade prompt and everything else.

**Top accent border pattern** for featured cards:
```css
border-top: 2px solid rgba(236,204,48,0.5);  /* golden — for now-playing */
```

With a matching gradient pseudo-element:
```css
.card::before {
  content: ''; position: absolute; top: -1px; left: 22px; right: 22px; height: 2px;
  background: linear-gradient(90deg, transparent, rgba(244,208,72,0.7), transparent);
}
```

Left accent stripe (now-playing card only):
```css
.now-playing::after {
  content: ''; position: absolute; top: 0; left: 0; bottom: 0; width: 3px;
  border-radius: 10px 0 0 10px;
  background: linear-gradient(180deg, var(--sun2) 0%, rgba(236,204,48,0.1) 100%);
}
```

### Eyebrow labels

Small all-caps label above a heading:
```css
font-size: 9px; font-weight: 700; letter-spacing: 0.18em; text-transform: uppercase;
color: var(--sun2);   /* golden for music sections */
/* or */
color: rgba(245,237,216,0.4);   /* muted for structural labels */
```

### Play button

Golden sun — not red, not orange. The sun in the image is yellow-gold.
The outer ring (`0 0 0 6px`) gives it a halo effect — like light bleeding around the sun.

```css
.play-btn {
  width: 48px; height: 48px; border-radius: 50%;
  background: var(--sun2);   /* #ECCC30 */
  color: var(--shadow);      /* dark text — readable */
  box-shadow:
    0 4px 20px rgba(236,204,48,0.5),     /* drop shadow */
    0 0 0 6px rgba(236,204,48,0.1),      /* outer ring halo */
    0 0 28px rgba(236,204,48,0.2);       /* diffuse bloom */
}
.play-btn:hover {
  box-shadow:
    0 4px 24px rgba(244,208,72,0.65),
    0 0 0 8px rgba(244,208,72,0.14),
    0 0 36px rgba(244,208,72,0.25);
  transform: scale(1.05);
}
.play-btn.playing {
  background: var(--ok);     /* #2563EB — blue when streaming */
  color: #fff;
  box-shadow: 0 4px 20px rgba(37,99,235,0.5), 0 0 0 6px rgba(37,99,235,0.1);
}
```

**Never use green for the playing state.** Blue (`--ok`) only.

### Waveform

36 golden bars, each bouncing on its own random interval. All the same color — no
played/upcoming progress. The randomness is the visual.

```css
.waveform { display: flex; align-items: center; gap: 3px; height: 28px; padding-left: 14px; margin-top: 18px; }
.wb { width: 3px; border-radius: 2px; background: rgba(240,200,64,0.45); animation: wv var(--d) ease-in-out infinite alternate; animation-delay: var(--dl); }
@keyframes wv { from { height: 3px; } to { height: var(--h); } }
```

Generated with JS — each bar gets randomized `--h`, `--d`, `--dl` CSS props:

```js
for (let i = 0; i < 36; i++) {
  const b = document.createElement('div');
  b.className = 'wb';
  const h = 4 + Math.random() * 20;
  b.style.cssText = `--h:${h}px;--d:${(0.45+Math.random()*0.85).toFixed(2)}s;--dl:${(Math.random()*0.7).toFixed(2)}s;height:${Math.round(h*0.4)}px`;
  wv.appendChild(b);
}
```

Add `.paused` class on the `.waveform` element when not playing; pair with:
`waveform.paused .wb { animation-play-state: paused; }`

### Ticker (flow text beneath player)

Inline in the document flow, right after the now-playing card. Uses CSS `mask-image`
to fade the text at both edges — avoids hard clipping.

```css
.ticker-wrap {
  position: relative; overflow: hidden;
  background: rgba(42,16,8,0.2);
  border-bottom: 1px solid rgba(245,237,216,0.1);
  -webkit-mask-image: linear-gradient(90deg, transparent 0%, black 6%, black 94%, transparent 100%);
  mask-image: linear-gradient(90deg, transparent 0%, black 6%, black 94%, transparent 100%);
}
.ticker {
  display: flex; white-space: nowrap;
  padding: 10px 0;
  animation: ticker-scroll 32s linear infinite;
}
.ticker span {
  font-size: 11px; font-style: italic; font-family: 'Playfair Display', serif;
  color: rgba(245,237,216,0.5); padding: 0 8px;
}
.ticker span::before { content: '·'; margin-right: 8px; color: rgba(240,200,64,0.4); }
@keyframes ticker-scroll { from{transform:translateX(0)} to{transform:translateX(-50%)} }
```

Content: duplicate the item array so the scroll loops seamlessly (50% scroll = full loop).
Show on `display:none` initially; reveal once station data arrives via `_initTicker()`.

**Never use `position: fixed` for the ticker** — it should feel like part of the player, not a news header.

**Ticker CTA injection**: when Spotify is not connected, two golden CTA items are mixed
into the ticker among the ambient Italian text: "♫ Porta i tuoi dischi — connetti Spotify"
and "♫ Your music, same hosts — connect Spotify". These use `--sun2` color instead of the
normal muted cream, making them stand out without breaking the ambient feel. Clicking scrolls
to the connect card. CTA items are removed once Spotify connects.

```css
.ticker .ti.ticker-cta { color: var(--sun2); cursor: pointer; }
.ticker .ti.ticker-cta:hover { color: var(--sun); }
```

### Connect CTA (cream contrast card)

The Spotify connect prompt uses an **inverted cream card** — the only light-background element
in the UI. This deliberately breaks the sienna card pattern to create visual hierarchy: the
upgrade CTA must never look like a regular card or an error state.

```css
.connect-cta {
  background: var(--cream);        /* #F5EDD8 — inverted from normal cards */
  border-left: 4px solid var(--sun);  /* golden accent stripe */
  border-radius: 10px;
  padding: 16px 20px;
  display: flex; align-items: center; gap: 14px;
  box-shadow: 0 2px 16px rgba(42,16,8,0.2);
}
```

- **Headline**: Playfair Display 17px bold italic, `--shadow` dark text
- **Subtitle**: Inter 11px, `rgba(42,16,8,0.55)`
- **Hover**: `translateY(-1px)` lift with deeper shadow
- **Icon**: music note emoji left, chevron arrow right
- Hidden when `spotify_connected` is true

**Design rationale**: the council review identified that the old "Bring Your Records" card
had identical visual weight to the "Having trouble?" error state. The cream card solves this
with a single CSS-level change — no layout restructuring needed.

### Help toggle (collapsed troubleshooting)

Troubleshooting is demoted from a full card to a `(?)` icon with expandable popover.
This prevents the error/help state from competing with the upgrade CTA.

```css
.help-toggle {
  font-size: 11px; color: rgba(245,237,216,0.35);
  /* centered, minimal — not a card */
}
.help-popover {
  background: var(--sienna);
  border: 1px solid rgba(245,237,216,0.12);
  border-radius: 8px; padding: 14px 16px;
}
```

- Toggle text: "(?) Having trouble connecting?"
- Popover contains WiFi instructions + "Advanced options" link
- After 60s without connection, toggle text brightens to `rgba(245,237,216,0.55)`
- "Advanced options" is *only* accessible through this path — never shown as a standalone button

### FM dial band

Dark interior only — this is the one place `--shadow` (`#2A1008`) is correct.

```css
.dial-band {
  background: var(--shadow);
  border: 1px solid rgba(245,237,216,0.08);
  border-radius: 6px;
}
```

Needle: Lancia red with glow.

```css
.dial-needle {
  width: 2px;
  background: linear-gradient(180deg, var(--lancia2), rgba(160,32,24,0.5));
  box-shadow: 0 0 8px rgba(184,44,32,1), 0 0 20px rgba(184,44,32,0.5);
}
.dial-needle::before {
  /* teardrop top */
  content: ''; width: 10px; height: 10px; border-radius: 50%;
  background: var(--lancia2);
  box-shadow: 0 0 10px rgba(184,44,32,0.9), 0 0 20px rgba(184,44,32,0.4);
}
```

### Buttons

Primary: golden, dark text.

```css
.btn {
  background: var(--sun2); color: var(--shadow);
  font-weight: 600;
  box-shadow: 0 2px 12px rgba(236,204,48,0.3);
}
```

Ghost: cream outline, transparent.

```css
.btn-ghost {
  background: rgba(245,237,216,0.08);
  border: 1px solid rgba(245,237,216,0.2);
  color: rgba(245,237,216,0.7);
}
```

**Never red buttons** for primary actions — red is Lancia interactive (dial, connect border).

### Live chip / status badges

```css
.live-chip {
  padding: 5px 12px; border-radius: 20px;
  background: rgba(42,16,8,0.3);
  border: 1px solid rgba(245,237,216,0.25);
  font-size: 9px; font-weight: 700; letter-spacing: 0.14em; text-transform: uppercase;
}
.live-dot {
  width: 6px; height: 6px; border-radius: 50%;
  background: var(--sun);
  box-shadow: 0 0 8px var(--sun);
  animation: blink 1.4s ease-in-out infinite;
}
```

Tier badge uses `--ok` (blue) for connected states. Keeps semantic color discipline.

### Inputs

```css
.input {
  background: rgba(42,16,8,0.4);
  border: 1px solid rgba(245,237,216,0.2);
  color: var(--cream);
}
.input:focus { border-color: rgba(236,204,48,0.6); }
```

Dark interior, cream text, golden focus ring.

---

## Approved look — do not drift from this

The current dashboard design was iterated to an approved state. These elements are correct
and should be preserved exactly when touching related code:

- **Background**: warm orange-red sunset gradient — large, atmospheric, dominant. Not dark.
- **Cards**: deep opaque sienna surfaces — solid depth, no transparency.
- **Connect CTA**: cream contrast card — the one deliberate exception to sienna cards. Golden left stripe, Playfair italic headline, dark text on cream. Must pop.
- **Help toggle**: `(?)` icon, not a card. Troubleshooting and advanced options live behind this toggle only.
- **Waveform**: 36 golden bars bouncing independently — organic, not a progress bar.
- **Ticker**: Playfair italic Italian text flowing left below the player — unhurried (32s loop). Golden CTA items injected when Spotify is disconnected.
- **Play button**: golden with three-layer glow (drop shadow + outer ring + diffuse bloom).
- **Dial needle**: Lancia red glow, overshoot-and-settle locking animation.

When in doubt: reference the file at `/tmp/platinum-designs/variant-VOLARE.html` and
the Volare record cover image at `.context/attachments/image-v1.jpg`.

---

## Motion

### Ticker scroll

32 seconds for a full loop. This is intentionally **slow** — it's ambient information, not
urgent news. The text should feel like it's drifting past, not racing. Do not speed it up.

The `ease-in-out infinite` on the individual bar animations and `linear infinite` on the
ticker scroll are different by design:
- Ticker: `linear` — constant, hypnotic drift
- Waveform bars: `ease-in-out alternate` — each bar breathes, bounces at its own pace

The mask-image fade (6% either side) means the text appears and disappears softly — never
hard-clips at the edge.

### Waveform smoothness

Each bar has randomized `--d` (duration: 0.45–1.3s) and `--dl` (delay: 0–0.7s) so they
never sync up. The lack of synchronization is what makes it look organic rather than
mechanical. Do not regularize the timing.

Initial bar height is set to ~40% of max (`height: Math.round(h * 0.4)`) so bars start
mid-height and can bounce both up and down. Starting at 0 would make them all jump from
the bottom on load — too abrupt.

### Track title change

When the now-playing track updates (`np-track` textContent change), the new text simply
replaces inline. No slide or fade needed — the Playfair Display italic at 20px has enough
visual presence that the eye catches it without animation. Adding a transition would
compete with the waveform motion.

### Radio dial seeking

Organic momentum-based drift — `_dialSeek()` in `dashboard.html`. Do not simplify
to linear animation. The wobble is the feel.

### Dial lock overshoot

`_dialLock()`: overshoot ±1.2 MHz, then settle in two steps (400ms, 800ms total).
This is the moment the station "locks in." Keep it.

### Tier badge pulse

```css
@keyframes tier-pulse {
  0%   { transform: scale(1); opacity: 0.7; }
  40%  { transform: scale(1.1); opacity: 1; }
  100% { transform: scale(1); opacity: 1; }
}
```

Fires once on tier change. Not a loop.

### Transition overlay

```css
.transition-overlay {
  background: rgba(196,64,32,0.85);   /* warm terracotta, not charcoal */
}
.transition-overlay h2 {
  font-family: 'Playfair Display'; font-style: italic; font-size: 32px;
  text-shadow: 0 2px 20px rgba(42,16,8,0.5);
}
```

Used for "Benvenuto!" on Spotify connect. Keep the warm overlay, not dark.

---

## Rules and anti-patterns

| Rule | Reason |
|------|--------|
| Never semi-transparent card backgrounds | Causes "verschwommen" (blurry) effect — warm bg bleeds through |
| Never multiple radial glow blobs on body | Same blurry effect — use a single directional linear gradient |
| Never green for success / connected / playing | Red-green colorblind. Use `--ok` (#2563EB) |
| Never cold grey or charcoal as the dominant background | This is Italian radio, not a SaaS dashboard |
| Never Inter for the station name | Always Playfair Display italic |
| Never light backgrounds on cards (except Connect CTA) | Cards are "buildings in shadow" — the cream CTA is the sole exception, for hierarchy |
| Never a full card for troubleshooting/error states | Demote to `(?)` toggle — error UI must never compete with upgrade prompts |
| Always pair semantic color with a shape icon | `.status-ok::before { content: "✓" }` — not color alone |

---

## Colorblind accessibility

Florian is **red-green colorblind**. This affects all UI work:

- Success / connected / active → `#2563EB` (blue), never green
- Error → `#c44a4a` (red — only for errors, not active states)
- Warning → `#c4a04a` (amber)
- Teal is also hard to read — avoid

All semantic states must pair a color with a **shape**: checkmark (✓), triangle (△),
cross (✗), or circle (○). Color alone is never sufficient for status communication.

---

## Listener page

`listener.html` is the public-facing stream page. It shares the brand but is simpler —
no admin controls, no dial animation. When restyling, apply the same Volare palette
but check that the stream embed and now-playing elements remain fully functional.

---

## File locations

| Asset | Path |
|-------|------|
| Dashboard | `mammamiradio/dashboard.html` |
| Listener | `mammamiradio/listener.html` |
| Logo SVG | `mammamiradio/logo.svg` |
| HA add-on icon | `ha-addon/mammamiradio/icon.png` (256px) |
| HA logo | `ha-addon/mammamiradio/logo.png` (512px) |
| Volare reference image | `.context/attachments/image-v1.jpg` |
| Prototype HTML | `/tmp/platinum-designs/variant-VOLARE.html` — the approved design iteration (not committed, local only) |

To regenerate HA add-on PNGs from SVG:
```bash
cairosvg mammamiradio/logo.svg -o ha-addon/mammamiradio/icon.png -W 256 -H 256
cairosvg mammamiradio/logo.svg -o ha-addon/mammamiradio/logo.png -W 512 -H 512
```
