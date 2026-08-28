# Banter authoring kit

Developer-only workflow for writing, rendering, and reviewing timeless host
banter through the station's real live chain, then listening in a self-contained
offline board. This is not a content factory: there is no service, queue,
promotion protocol, or automatic runtime installation. Human listening remains
the creative gate.

```bash
python scripts/banter-workshop.py render --plan scripts/banter-pack-v1.json --output tmp/banter-workshop-run
python scripts/banter-workshop.py board --report tmp/banter-workshop-run/review-report.json --output tmp/banter-workshop-run/listening-board.html
python scripts/banter-workshop.py board --from-accepted-baseline --shipped-audio --output tmp/banter-accepted-board/listening-board.html
python scripts/banter-workshop.py sync-check
```

Serve an ordinary board from the report directory. Serve a shipped-audio board
from the repository root so relatives into `mammamiradio/assets/demo/banter/`
resolve. Ordinary boards must be written next to the report; output elsewhere
is rejected. The board is offline: Keep / Maybe / Redo, notes, import/export
JSON, and exact-track predecessor playback. Feedback is bound to actual MP3
bytes and namespaced in localStorage. Reset writes a cleared tombstone so reload
does not restore embedded accepted verdicts.

`scripts/banter-pack-v1.json` holds the 21 accepted creative directions.
`scripts/banter-pack-v1-feedback.json` holds accepted Keep decisions and notes.
`sync-check` fails closed if SHA, mode, predecessor ID, or special status drift
from `spoken_assets.json`. Do not commit a second copy of the shipped MP3s.

Live `write_banter` is unchanged unless `packaged_context` / `require_generated`
are set. Packaged authoring suppresses listener-request injection. Evergreen
rows get no predecessor track. Exact-track rows receive one approved starter's
title and artist. Clock, weather, and unprovided sonic claims fail closed.
Creative directions are rejected if they exceed the shared character limit;
they are never silently truncated. Tests mock paid providers.
