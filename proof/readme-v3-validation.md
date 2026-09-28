# README and interactive explainer validation

Validated locally on 2026-09-13 against main baseline
`ad7a384222e8a8022ddf50086eba0b8e4e9d140f`.
This is development-candidate documentation, not proof of a v3 release,
live deployment, Home Assistant hardware behavior, or human listening.

## Checks

| Check | Observed result |
| --- | --- |
| `env COVERAGE_RATCHET_XDIST=4 make check` | 9,421 passed, 4 skipped; 92.63% coverage; all floors held |
| Focused Home media-source and First Listen narration tests | 169 passed, 6 FFmpeg-marked tests deselected |
| Explainer `npm test` | 35 passed |
| Explainer `npm run build` | Passed |
| Explainer `npm run test:e2e` | 9 passed |
| `scripts/validate-spoken-assets.py` | All packaged and browser spoken-asset contracts passed |
| Documentation links, version sync, gitleaks, `git diff --check` | Passed |
| `bash -n` on tracked shell scripts | 63 passed |

The browser regression covers blocked playback followed by a successful retry:
no success claim or played-moment credit before the cue, then exactly one
credited moment. Python CI now compares both scenario ID sets and each
scenario's quote/reachability with the app's Home-moment pack.

## Browser and content scope

Chromium checks covered the four selectors, first activation, cue-driven
reveal, transcript fallback/retry, tour completion, install-link destination,
keyboard activation, and reduced motion. Desktop (1440 × 1000) and phone-sized
(390 × 844) views were inspected; no horizontal overflow or console errors
were observed. Phone inspection used emulation, not physical-device Safari.
The README was inspected as locally rendered Markdown with GitHub-like styles,
not as a deployed GitHub page. First Listen screenshot state was synthetic.

The public recordings remain invented examples, not a live Home connection.
Fresh-install sharing stays off by default; after opt-in its authorization is
limited to daylight and normalized weather. The staged sunset's precise values
are not a literal preview of that projection. No audio or video bytes changed.

## Screenshot inventory

| File under `docs/screenshots/` | Dimensions | Bytes |
| --- | --- | ---: |
| `01-house-made-it-on-air.webp` | 1440 × 840 | 300,914 |
| `02-first-listen-private.webp` | 832 × 1096 | 32,394 |
| `03-producer-desk.webp` | 1000 × 860 | 130,420 |

Total: 463,728 bytes, all additions relative to the main baseline. The refreshed
First Listen image is 304,432 bytes smaller than its previous 336,826-byte
version. No canonical screenshot-byte or page-performance budget was found;
these measurements do not establish a performance pass. The existing 30-second
pitch target still requires human comprehension review.
