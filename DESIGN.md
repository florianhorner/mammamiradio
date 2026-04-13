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
  --surface:       #251E19;   /* card backgrounds */
  --surface-hover: #2E2520;   /* interactive hover */
  --surface-strong:#362B25;   /* emphasized surfaces */

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
  --line:          rgba(245,237,216,0.10);  /* borders, dividers */
  --line-strong:   rgba(245,237,216,0.16);  /* focused input borders */
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

### Waveform
36 golden bars bouncing independently. Each bar has randomized duration (0.45–1.3s) and delay (0–0.7s). The lack of sync is what makes it organic.
```css
.waveform { display: flex; align-items: center; gap: 3px; height: 28px; }
.wb { width: 3px; border-radius: 2px; background: rgba(240,200,64,0.4);
      animation: wv var(--d) ease-in-out infinite alternate; animation-delay: var(--dl); }
@keyframes wv { from { height: 3px; } to { height: var(--h); } }
```
Add `.paused` class when not playing: `.waveform.paused .wb { animation-play-state: paused; }`

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
Every card surface is **opaque** `--surface` (#251E19). Never semi-transparent.
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

### Status badges
Always pair color with shape icon. Color alone is never sufficient.
```css
.badge { padding: 4px 12px; border-radius: 20px; font-size: 10px; font-weight: 700;
         letter-spacing: 0.12em; text-transform: uppercase; }
.badge-ok { background: rgba(37,99,235,0.12); border: 1px solid rgba(37,99,235,0.3);
            color: var(--ok); } /* prefix: ✓ */
.badge-live { background: rgba(236,204,48,0.12); border: 1px solid rgba(236,204,48,0.3);
              color: var(--sun2); } /* prefix: pulsing dot */
.badge-warn { background: rgba(217,119,6,0.12); border: 1px solid rgba(217,119,6,0.3);
              color: var(--warning); } /* prefix: △ */
.badge-err { background: rgba(196,74,74,0.12); border: 1px solid rgba(196,74,74,0.3);
             color: var(--error); } /* prefix: ✗ */
```

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

Florian is **red-green colorblind**. This affects all UI work:

- Success / connected / active: `#2563EB` (blue), never green
- Error: `#c44a4a` (red — only for errors, not active states)
- Warning: `#D97706` (amber)
- Teal is also hard to read — avoid

All semantic states must pair a color with a **shape**: checkmark (✓), triangle (△), cross (✗), or circle (○). Color alone is never sufficient for status communication.

## File locations

| Asset | Path |
|-------|------|
| Dashboard | `mammamiradio/dashboard.html` |
| Listener | `mammamiradio/listener.html` |
| Logo SVG | `mammamiradio/logo.svg` |
| HA add-on icon | `ha-addon/mammamiradio/icon.png` (256px) |
| HA logo | `ha-addon/mammamiradio/logo.png` (512px) |
| Design preview | `/tmp/design-preview-volare-refined.html` (local only, not committed) |

To regenerate HA add-on PNGs from SVG:
```bash
cairosvg mammamiradio/logo.svg -o ha-addon/mammamiradio/icon.png -W 256 -H 256
cairosvg mammamiradio/logo.svg -o ha-addon/mammamiradio/logo.png -W 512 -H 512
```

## Decisions Log
| Date | Decision | Rationale |
|------|----------|-----------|
| 2026-04-11 | Unified espresso-dark palette, replacing split Volare sunset + dark listener palettes | Listener and admin felt like two different products. Category standard (NTS, SomaFM) uses dark backgrounds. Italian warmth preserved via accents, typography, and surface materials. |
| 2026-04-11 | Kept Playfair Display italic for display | Brand equity from live tests — "Mamma Mi Radio" in Playfair italic IS the identity |
| 2026-04-11 | Replaced Inter with Outfit for body | Inter is overused and generic. Outfit is warmer, more geometric, with enough personality for an Italian radio station |
| 2026-04-11 | Added JetBrains Mono for technical readouts | Timestamps, bitrates, queue depth, diagnostics. Clear signal that this data is for the operator. |
| 2026-04-11 | Reviewed by Codex (GPT-5.4) and Claude subagent | Both independently converged on the same core direction: warm dark, serif display, unified palette. Codex proposed Fraunces, subagent proposed Cormorant Garamond. User chose to keep Playfair. |
