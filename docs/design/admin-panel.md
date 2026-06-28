# Admin Panel Standards
<!-- version: 1 | owner: @florianhorner -->

Every PR that modifies `mammamiradio/web/templates/admin.html` or `mammamiradio/web/templates/listener.html` must pass this checklist before merge. CI enforces acknowledgment of this section in the PR body.

## Protected UI elements

These have regressed in past refactors. Verify each protected element survives every HTML edit.

| Element | File | How to check |
|---|---|---|
| Token cost counter + split | `admin.html` Engine Room | Grep for `api_cost_estimate_usd` and `cost_breakdown`; verify the rendered Costi group shows the session total and category split |
| Play button blue state | `static/base.css` | `.play-btn.playing` uses `var(--ok)` (blue `#2563EB`), never `var(--sun2)` (golden) |
| Station name from localStorage | `static/listener.js` | JS reads `localStorage.getItem('stationName')`; admin panel writes it |
| Gold "Mi" accent | `admin.html`, `listener.html` | `<span class="mi">` present in `<h1>`, styled `color: var(--sun)` |
| Italian tricolor stripe | `admin.html` (`.tricolor-stripe`), `listener.html` (`.tricolor-band`) | tricolor div present below `<h1>` on each surface |

## Colorblind safety

Florian is red-green colorblind. This is non-negotiable.

- **Never use green** for success, connected, or positive states
- Use **blue (`#2563EB` / `var(--ok)`)** for success/connected
- Pair all semantic colors with shape icons (checkmark, triangle, dot)
- Acceptable palette: blue, amber, red

## Design system

See `system.md` (sibling) for the full system. Admin panel must use:

- **Background:** espresso dark `#14110F` with warm gradient
- **Cards:** warm brown `#251E19`
- **Accent:** golden sun `#F4D048` / `#ECCC30`
- **Text:** cream `#F5EDD8`
- **Fonts:** Playfair Display italic (display) · Outfit (body) · JetBrains Mono (technical)

## Information Architecture — Producer Desk

The admin panel is a **producer desk**: opening `/admin` answers two questions
fast — is the station alive and sounding right, and what plays next. Set-and-forget
config and debug never occupy the default view.

**Default view — desktop-pinned live console + tabbed work area.**

On desktop, the **live deck** (`.mmr-deck`, containing `.mmr-console` + `.mmr-tabbar`)
is pinned to the top and never scrolls away. It carries the whole live glance in
one block:

- **Left:** now-playing (segment type `.status-chip`, title, artist, progress),
  Skip / Stop, and the compact token cost counter. There is no "session N" counter
  — that number meant nothing to an operator.
- **Right:** the four **air-next** triggers (Banter / Ad break / News flash / More
  chaos) and the live **In Produzione** "cooking now" feed (per-segment phase +
  label). This is the only place an operator sees work in flight, so it stays in
  view at all times. On wider screens the four triggers sit in a single horizontal
  row (`grid-template-columns: repeat(4,1fr)`); on ≤900px they wrap to two columns.

**Idle-state compact console.** While the station has not yet produced its first
segment, the console carries the CSS class `is-idle` (set in the initial HTML
markup; removed by `updateNow()` when the first segment appears; re-added by
`updateNow()` when `ns` is null). In idle state `.mmr-air-title` shrinks to 16px
plain text (muted colour), `.mmr-air-artist` and `.mmr-air-progress` are hidden, so
the section tabs and Live Queue are visible without scrolling. When the first segment
arrives `is-idle` is removed and the console expands to full height automatically.
If `refreshFast()` errors, the catch block also removes `is-idle` so a network
failure never leaves the console permanently compact.

Below the console, a **tab bar** swaps a single **work area** — one panel visible
at a time, choice persisted in `sessionStorage['adminTab']`:

1. **Diretta** — `Modalità live` (Chaos/Festival toggles), `Azioni rapide` (fewer
   banter / fewer ads / reload / flag, plus a Lancia-red `Purge queue`), `Cadenza`
   (pacing sliders). The `Azioni immediate` triggers moved up into the console.
2. **Scaletta** (default tab) — forward-only rundown of up to ~8 upcoming items,
   each with a compact relative label (`next` / `after` / `later`) + rough duration.
   Pending listener requests sit in a strip at the top and collapse when empty. No
   played history.
3. **Rotazione** — searchable/prunable music library. Row checkboxes select songs
   to ban; the selection is keyed by artist/title and survives the 3s status poll
   (it used to be wiped on every rebuild).
4. **Conduttori** — host personality config. Active preset chips carry a checkmark
   shape cue alongside the gold fill (colorblind safety).
5. **Archivio** — filterable segment history. Search box + type chips
   (All/Music/Hosts/Ads/News) + time chips (Last hour/Today/All available). Filter
   state persists via sessionStorage (`mmr.admin.archivio.filters`).
6. **Motore** (diagnostics) — `Status` (systems, runtime health, capabilities, HA
   context), `Costi` (token cost counter + cost split + segment counts — always visible),
   `Configurazione` (station behavior controls), and `Setup` (a collapsible
   `<details>` that auto-collapses when every readiness item is ready; shows an
   `All ready ✓` blue badge when collapsed).

Each panel carries exactly one header (Playfair title); the old per-drawer summary
plus inner-panel double header is gone. On narrow viewports the console stacks to
one column, the tab bar wraps into compact rows without exposing a horizontal
scrollbar, and the full upper deck scrolls away with the page so it cannot cover
the active panel.

**Destructive actions use a 5s undo toast** (`undoableToast` in
`static/admin.js`): the row is removed optimistically, the backend call is
deferred for the undo window, and Undo cancels it. Stack capped at 5 toasts.

**Labels are English-first operator copy, with Italian reserved for structural
section names and on-air flair** — independent of the super-italian toggle. This
mirrors the `MAMMAMIRADIO_SUPER_ITALIAN` OFF contract (English utility copy,
Italian headlines and station-feel words).

- **Italian (flair):** tab / section names (`Diretta`, `Scaletta`, `Rotazione`,
  `Conduttori`, `Motore`, `Archivio`), the `Regia` eyebrow, Diretta subgroup
  eyebrow labels (`Modalità live`, `Azioni rapide`, `Cadenza`), the console
  `In produzione` and `In onda` / `Fermo` on-air labels, and the `Anni '70/'80/'90`
  era chips.
- **English (utility):** every button, tooltip, toast, form subhead, search
  state, empty state, status label, and helper line.
- Regression guard: `tests/web/test_admin_regia_polish.py::test_no_italian_utility_strings_remain`.

## Interaction standards

- Minimum touch target: 44px on control buttons, filter chips/pills, and section tabs
- Every destructive action (purge, stop, delete) must show a toast confirmation
- Sliders must update their visual track fill immediately on change
- Admin controls must show feedback within 300ms of user action (toast, state change, or loading indicator)
- **Accessibility structure:** the listener page exposes a `<main id="content">` landmark with a skip link, and its `<html lang>` follows the active copy register (it/en) — admin stays `lang="it"`. A stopped session is baked into the first server paint (`body[data-stopped]` + `is-stopped` + paused waveform) so the page never flashes "live" before JS hydrates. Admin section tabs implement the ARIA tablist/tab/tabpanel pattern (roving focus, Left/Right/Home/End arrow-key navigation, `aria-selected`), and the brand wordmark is the page `<h1>`. Chips, pills, and section tabs all meet the 44px minimum — the same floor as control buttons.

## QA requirement

Before merging any admin panel PR:
1. Run `/qa` on `/admin` (operator-facing: controls, sliders, host config, engine room, playlist)
2. Run `/qa` on `/` (listener-facing: stream playback, now-playing, up-next, responsive layout)

Both must pass. A single combined run is insufficient.

## PR checklist (copy into PR body)

```
## Admin Panel Standards
- [ ] Token cost counter (`api_cost_estimate_usd`) and cost split (`cost_breakdown`) still visible in Engine Room
- [ ] Play button uses `var(--ok)` (blue) for playing state — not golden
- [ ] Station name reads from `localStorage.stationName`
- [ ] `<span class="mi">` present in `<h1>` in every modified HTML file
- [ ] Tricolor div present below `<h1>` in every modified HTML file (`.tricolor-stripe` on admin, `.tricolor-band` on listener)
- [ ] No green used for any success/connected state (colorblind safety)
- [ ] Player QA run passed on `/`
- [ ] Admin QA run passed on `/admin`
```
