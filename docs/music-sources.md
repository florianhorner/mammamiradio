# Music sources and rights boundaries

This is the canonical guide to where Mamma Mi Radio gets music, what the
software can prove, and what remains the station operator's responsibility.
It describes technical and attribution controls; it is not legal advice or a
certification that a particular broadcast is cleared in every jurisdiction.

## Source summary

| Source | Default | Persistence | What the project asserts |
| --- | --- | --- | --- |
| Bundled starter collection | On | Packaged with the app | Exact files and attribution are hash-pinned in the release manifest. Every derivative is attribution-only — CC BY 4.0 from Incompetech, CC BY 3.0 from Jamendo — and carries a modification notice. |
| Operator `music/` files | Available when mounted | Operator-managed | Mamma Mi Radio makes no rights claim. The operator is responsible for the files and their use. |
| Jamendo transient expansion | Off | No audio or lease persistence | Jamendo reports the accepted track's source and CC BY 3.0/4.0 license. That report is attribution data, not a clearance verdict. |
| External extraction | Off; standalone extra only | Normal standalone cache rules | Technical access is not permission. Both current Home Assistant add-ons omit yt-dlp entirely. |

The code is Apache-2.0. That license does not replace the licenses attached to
audio, provider terms, public-performance requirements, or the operator's own
obligations.

## Bundled starter collection

The offline starter collection contains twelve tracks from two sources.

Six from Incompetech under **CC BY 4.0**, all by Kevin MacLeod: Long Time Coming, Realizer, Newer Wave, Laserpack, Andreas Theme, Limit 70.

Six from Jamendo under **CC BY 3.0**: Stuttgart (Portrayal), Smallest - Stories (Smallest), The Tide (Square a Saw), Dance with me (Manhat10), Stitches ft. Shane MauX (Lilly Wolf), Set Me On Fire (feat. Ashley Jana) (David Amber).

Both tiers are attribution-only. Nothing carrying a NonCommercial,
NoDerivatives or ShareAlike term can enter the bundle, and NoDerivatives is the
clause that disqualifies otherwise-suitable tracks: every bundled derivative is
normalized to a fixed loudness and re-encoded, which ND forbids distributing.

The single authority is
`mammamiradio/assets/starter/catalog.json`. It records the source page, exact
source and derivative hashes, ISRC, duration, license, attribution, acquisition
evidence, and modification notice. Runtime track metadata, listener credits,
package checks, and release proof derive from that manifest; there is no second
hand-maintained starter list.

The distributed files are normalized and transcoded for Mamma Mi Radio with no
musical edits. A release must prove all of the following:

- exactly 12 approved derivatives and no undeclared packaged tracks;
- at least 45 minutes total and no more than 75 MiB;
- 48 kHz, stereo, 192 kbps MP3 at the station loudness target;
- matching hashes, complete source evidence, and a full human audition record;
- 20 cold Home Assistant Green runs with the first accepted non-silent starter
  byte reaching a connected listener at p95 no slower than two seconds;
- every track plays once before the starter cycle repeats.

Until the exact derivatives, hashes, acquisition evidence, and complete
audition records are present, the strict release gate remains red. Tooling does
not fabricate or infer those human decisions.

The ordinary pull-request gate runs one cold aarch64 launch in
`.github/workflows/pi-smoke.yml`; it does not require physical-device receipts.
Finalize version, changelogs, and V1/V2 preship evidence before recording.
For a stable release, start from the exact clean commit running on Home
Assistant Green and record twenty cold runs locally on that device:

```bash
for run in $(seq 1 20); do
  python3.11 -P scripts/ha-green-launch-smoke.py \
    --record-release-receipt proof/media/ha-green-release-evidence
done
make ha-green-release-proof
```

Receipt mode detects Home Assistant Green through the aarch64 device tree,
refuses a dirty checkout, blocks outbound network access during every run, and
creates one immutable JSON file without overwriting prior runs. A slow but
accepted, attributed, non-silent observation is recorded and the individual
command still exits red, so the release validator can calculate the real
nearest-rank p95 instead of silently dropping outliers.

Commit only `proof/media/ha-green-release-evidence/run-*.json` after the
measurements, then rerun `make ha-green-release-proof` and `make pre-release`.
The hardware-neutral `mammamiradio-release-content-v1` profile covers sorted paths, Git modes, and blob bytes, excluding only HA receipt JSON. This prevents both stale
evidence and the impossible self-reference of asking a committed receipt to
name the commit that contains itself. The tracked
`proof/media/ha-green-release-receipt.example.json` is explicitly marked as an
example and never counts toward a release.

## Operator-supplied local music

Files mounted in `music/` remain available as local music. They are labelled
"Provided by the station operator" in listener credits and do not receive a
project-clearance badge, receipt, or implied license. Upgrades and source
migrations never delete `music/`.

The stock Home Assistant add-on does not provide a general local-media upload
workflow. A standalone operator who mounts local files is responsible for
their provenance, licenses, and permitted use.

## Optional transient Jamendo expansion

Jamendo is an explicit, default-off option while written provider confirmation
for this station model is pending. To enable it, acknowledge that the API use
is non-commercial. This acknowledgement describes the operator's station, so
the checkbox is never preselected.

The station includes a Jamendo application ID. Jamendo issues one ID per
application, not per listener, and its terms reserve the right to delete
duplicate applications. Operators can provide their own ID for independently
authorized access; it overrides the bundled ID, and the admin panel shows which
one is active. Clearing it returns to shared access. A malformed environment or
startup-config override falls back with a warning; the admin rejects an invalid
new ID and keeps the current settings.

Jamendo counts requests per application or per requesting IP, and its reply
does not say which ceiling was reached. When the station is asked to slow down,
it reports that plainly and keeps retrying without presenting a credential
change as a fix.

Commercial, sponsored, affiliate, or monetized operation needs separate
authorization from Jamendo. Their radio documentation also requires a
commercial radio licence for direct or indirect commercial activity. Read the
[Jamendo API terms](https://devportal.jamendo.com/api_terms_of_use) before
enabling it.

Configure it in **Motore -> Setup -> Music sources**. On a fresh install the
card appears after the First Listen journey is finished, so the guided setup
keeps a single obvious action. Saving applies live and
does not restart or interrupt the station. A client ID saved by an older
version is imported to owner-only secrets where possible, remains disabled,
and requires a fresh acknowledgement before use. The admin UI never echoes the
ID. Bundled users see **Use own ID**; **Replace** and **Clear** appear only after an operator ID is saved.

The transient boundary is deliberately narrow:

- discovery requests Jamendo's streaming `audio` field with `audioformat=mp32`;
  `audiodownload` is never used;
- only validated CC BY 3.0 or CC BY 4.0 candidates with approved Jamendo and
  Creative Commons URLs are admitted;
- one provider operation and one app-owned audio artifact may exist across
  fetching, normalization, ready, queued, and playing states;
- HTTP response bytes stay under the size cap in memory until FFmpeg admission,
  then pass through a nonblocking pipe; there is no raw input file or persistent
  metadata sidecar;
- the normalized artifact authorizes one playback attempt in the current
  process and is deleted after play, cancellation, disablement, a source
  change, validation failure, shutdown, or startup pruning;
- Jamendo audio and lease data never enter the persistent cache, SQLite,
  restart handoff, continuity/rescue, clips, derivatives, or offline access;
- at most one prepared Jamendo track is inserted after two starter/local
  tracks. If none is ready, base music continues immediately. When the base
  rotation is empty (no starter or local tracks available), this cadence
  gate is bypassed and a prepared track may insert immediately — there is no
  base rotation left to wait for.

An adverse written provider response disables the integration pending an
explicit reassessment. A positive response may inform a later change, but it
does not retroactively turn provider-reported facts into a universal rights
clearance.

### Admin states

The source row uses five operational states. They describe preparation, not
copyright status.

| Visible state | Provider detail | Meaning and action |
| --- | --- | --- |
| `idle` | disabled | Jamendo is off; starter and local music continue. |
| `blocked` | needs configuration | Confirm non-commercial use; if included access is unavailable, add an operator client ID. |
| `working` | idle, discovering, fetching, normalizing, queued, playing, or consumed | One single-use track is being prepared; base music continues. |
| `ready` | ready | One track is prepared for one play, then deleted. |
| `degraded` | transient provider failure | Base music continues; use **Check again** when useful. |
| `blocked` | provider-wide configuration or contract rejection | The candidate cannot be used; check again or turn Jamendo off. |

Individual rejected candidates increment the coarse lifetime rejected count and
a per-pass breakdown keyed by failure code, while discovery continues. The
breakdown is what the admin row turns into a plain-language reason; it is
cleared when a pass succeeds, and each pass logs its own tally once. Status and
logs never expose the client ID, private stream URL, or raw provider exception.

### Configuration API

Authenticated operators can use:

```text
PUT  /api/media-sources/jamendo
POST /api/media-sources/jamendo/retry
```

`PUT` accepts `enabled`, `noncommercial_acknowledged`, optional `client_id`, and
optional `clear_client_id`. Omitting `client_id` retains it; a non-empty value
replaces it; `clear_client_id=true` removes the operator ID and returns the
station to bundled access when one exists. Clear is credential-only: inside the
serialized write it preserves the latest saved enablement and acknowledgement
instead of trusting possibly stale request values. Without bundled access,
clearing the last ID turns the source off. `jamendo_client_id_required` (409) is reachable only when no
bundled ID exists and the operator has not supplied their own ID. Replace and
clear cannot be requested together. Durable intent is saved before the live
provider changes.

`retry` returns `202` when enabled and coalesces concurrent attempts. When
disabled it returns `409 jamendo_retry_disabled`.

New media endpoint failures use this shape:

```json
{
  "ok": false,
  "error_code": "jamendo_ack_required",
  "message": "Confirm non-commercial API use before enabling Jamendo.",
  "field": "noncommercial_acknowledged",
  "retryable": false,
  "next_action": "Review and confirm the current non-commercial acknowledgement.",
  "stream_status": "unaffected"
}
```

Locked codes are `jamendo_invalid_request`, `jamendo_client_id_invalid`,
`jamendo_client_id_conflict`, `jamendo_client_id_required`,
`jamendo_ack_required`, `jamendo_retry_disabled`, and
`jamendo_config_save_failed`. External-only add-on operations return
`external_media_unavailable_in_addon`; the retired `jamendo://` source returns
`legacy_jamendo_source_retired`; ineligible music clips return
`music_share_unavailable`. In every case, starter/local playback is unaffected.

## Optional standalone external media

yt-dlp is not part of the default package. A standalone operator may install
the optional extra deliberately:

```bash
python -m pip install -e '.[external-media]'
export MAMMAMIRADIO_ALLOW_YTDLP=true
```

The status is `operator_enabled`, never `cleared`. The resolver can technically
reach many sites, but that says nothing about permission to access, download,
or broadcast their content. Both current Home Assistant add-ons omit the
distribution, importable module, and executable, ignore legacy enablement, and
keep local search available. Listener requests that would require extraction
remain shout-outs instead of hidden downloads.

## Attribution and sharing

The listener's **Music credits** dialog shows the current track separately from
the complete included catalog. Bundled music links to its own source — Incompetech
under CC BY 4.0, Jamendo under CC BY 3.0 — with the modification notice. Jamendo shows provider-reported public source
and license links. Local files show the operator-responsibility statement.
Unsafe or missing URLs render as neutral text without an anchor.

Only a complete, single bundled-track window is eligible for music sharing.
Jamendo, local, mixed, partial, and unknown windows return
`403 music_share_unavailable`. This restriction does not turn an eligible clip
into a broader legal clearance.

Maintainers changing the starter collection must follow
[Bundled starter media](../CONTRIBUTING.md#bundled-starter-media) and run both
`make media-check` and `make media-proof` before proposing a release.
