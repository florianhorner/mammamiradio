## Summary

<!-- What changed and why -->

## Test plan

<!-- How was this tested? -->

---

## Admin Panel Standards
<!-- Required when `mammamiradio/web/templates/admin.html` or `mammamiradio/web/templates/listener.html` changed. -->
<!-- CI will fail on admin panel PRs that omit this section. Skip if no HTML files changed. -->

- [ ] Token cost counter (`api_cost_estimate_usd`) still visible in Engine Room
- [ ] Play button uses `var(--ok)` (blue) for playing state — not golden
- [ ] Station name reads from `localStorage.stationName`
- [ ] `<span class="mi">` present in `<h1>` in every modified HTML file
- [ ] Tricolor div present below `<h1>` (`.tricolor-stripe` on admin, `.tricolor-band` on listener)
- [ ] No green used for any success/connected state (colorblind safety)
- [ ] Player QA run passed on `/`
- [ ] Admin QA run passed on `/admin`
