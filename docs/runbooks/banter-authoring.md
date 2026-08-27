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

# Rebuild the accepted first-pack board against shipped MP3s (no second copy)
python scripts/banter-workshop.py board \
  --report .context/plans/banter-authoring-kit-source/expanded-review-report.json \
  --feedback scripts/banter-pack-v1-feedback.json \
  --shipped-audio \
  --output tmp/banter-accepted-board/listening-board.html

# Fail closed if baseline IDs/hashes drift from spoken_assets.json
python scripts/banter-workshop.py sync-check
```

Serve the board directory locally (`python -m http.server` from the output
folder, or open the HTML file directly) and review in a browser. Feedback stays
in browser localStorage, namespaced by candidate-set identity. Export / import
JSON and Copy for chat — there is no network submission.

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
- Evergreen rows get no predecessor track and no live clock/weather/listener
  context.
- Exact-track rows receive exactly one approved starter's title and artist.
- Missing keys, provider failure, malformed/timelessness/sonic violations,
  exhausted attempts, unknown mode/context, or a non-empty output directory all
  fail closed with a non-zero exit.
- Tests mock paid providers; do not spend money merely to validate the kit.
