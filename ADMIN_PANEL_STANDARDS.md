# Admin Panel Standards
<!-- version: 1 | owner: @florianhorner -->

Every PR that modifies `mammamiradio/admin.html` or `mammamiradio/listener.html` must pass this checklist before merge. CI enforces acknowledgment of this section in the PR body.

## Protected UI elements

These have regressed in past refactors. Verify all five survive every HTML edit.

| Element | File | How to check |
|---|---|---|
| Token cost counter | `admin.html` Engine Room | Grep for `api_cost_estimate_usd`; verify it appears in the rendered status block |
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

See `DESIGN.md` for the full system. Admin panel must use:

- **Background:** espresso dark `#14110F` with warm gradient
- **Cards:** warm brown `#251E19`
- **Accent:** golden sun `#F4D048` / `#ECCC30`
- **Text:** cream `#F5EDD8`
- **Fonts:** Playfair Display italic (display) · Outfit (body) · JetBrains Mono (technical)

## Interaction standards

- Minimum touch target: 44px height on control buttons, 36px on chips/pills
- Every destructive action (purge, stop, delete) must show a toast confirmation
- Sliders must update their visual track fill immediately on change
- Admin controls must show feedback within 300ms of user action (toast, state change, or loading indicator)

## QA requirement

Before merging any admin panel PR:
1. Run `/qa` on `/admin` (operator-facing: controls, sliders, host config, engine room, playlist)
2. Run `/qa` on `/` (listener-facing: stream playback, now-playing, up-next, responsive layout)

Both must pass. A single combined run is insufficient.

## PR checklist (copy into PR body)

```
## Admin Panel Standards
- [ ] Token cost counter (`api_cost_estimate_usd`) still visible in Engine Room
- [ ] Play button uses `var(--ok)` (blue) for playing state — not golden
- [ ] Station name reads from `localStorage.stationName`
- [ ] `<span class="mi">` present in `<h1>` in every modified HTML file
- [ ] Tricolor div present below `<h1>` in every modified HTML file (`.tricolor-stripe` on admin, `.tricolor-band` on listener)
- [ ] No green used for any success/connected state (colorblind safety)
- [ ] Player QA run passed on `/`
- [ ] Admin QA run passed on `/admin`
```
