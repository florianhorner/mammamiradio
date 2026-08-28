# Banter authoring kit

Developer-only workflow for writing, rendering, and reviewing timeless host
banter through the station's real live chain (scriptwriter → Marco/Giulia TTS →
talk bed → normalization → audio validation), then listening in a self-contained
offline board.

This is **not** a content factory. There is no service, database, queue, worker
pool, resume engine, promotion protocol, or automatic runtime installation.

## Commands

```bash
# Render a plan (calls paid providers — only when you intentionally generate)
python scripts/banter-workshop.py render --plan scripts/banter-pack-v1.json \
  --output tmp/banter-workshop-run

# Rebuild the listening board from an existing report (no provider cost)
python scripts/banter-workshop.py board \
  --report tmp/banter-workshop-run/review-report.json \
  --feedback scripts/banter-pack-v1-feedback.json \
  --output tmp/banter-workshop-run/listening-board.html

# Rebuild the accepted first-pack board against shipped MP3s (no second copy).
# Derives portable report metadata from tracked plan + feedback + spoken_assets,
# so a clean checkout is enough — no gitignored Krakow artifacts required.
python scripts/banter-workshop.py board \
  --from-accepted-baseline \
  --shipped-audio \
  --output tmp/banter-accepted-board/listening-board.html

# Fail closed if plan/feedback/spoken IDs or hashes drift
python scripts/banter-workshop.py sync-check
```

Serve boards carefully:

- Ordinary workshop output: `python -m http.server` from the report/output
  folder (audio paths are rewritten relative to the board file).
- Accepted `--shipped-audio` boards: write the HTML under the repository (for
  example `tmp/banter-accepted-board/listening-board.html`) and serve from the
  **repository root** so the relative links into
  `mammamiradio/assets/demo/banter/` resolve. Opening the HTML file directly
  also works when the browser can resolve those relatives. Output paths outside
  the repo are rejected.

The board is fully offline: no Google Fonts or other network assets. Feedback
stays in browser localStorage, namespaced by candidate-set identity and bound to
each clip's audio sha256. Export / import JSON and Copy for chat — there is no
network submission. Changed audio under the same clip ID resets prior verdicts.

## Pack baseline

`scripts/banter-pack-v1.json` holds the 21 accepted creative directions.
`scripts/banter-pack-v1-feedback.json` holds the accepted Keep decisions and
notes. Treat them as the golden host-writing baseline (character fidelity,
Studio B lore, affectionate disagreement, natural escalation, clean endings,
restrained Giulia delivery, rare fourth-wall use, zero surveillance). Future
packs should learn from them, not imitate them mechanically.

Shipped audio already lives under `mammamiradio/assets/demo/banter/`. Do not
commit a second copy of those MP3s.

## Guards

- Default live `write_banter` behavior is unchanged unless
  `packaged_context` / `require_generated` are set.
- Packaged authoring always suppresses listener-request injection, even if the
  caller leaves `include_listener_request=True`.
- Evergreen rows get no predecessor track and no live clock/weather/listener
  context.
- Exact-track rows receive exactly one approved starter's title and artist.
- Missing keys, provider failure, malformed/timelessness/sonic violations,
  exhausted attempts, unknown mode/context, or a non-empty output directory all
  fail closed with a non-zero exit.
- Tests mock paid providers; do not spend money merely to validate the kit.
