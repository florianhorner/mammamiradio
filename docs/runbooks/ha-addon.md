# HA Addon Release Runbook

How to release a new version of the Mamma Mi Radio Home Assistant addon without breaking anything.

## The release chain

```
Code change
  → merge to main (version files unchanged; main advertises the last published version)
  → soak on edge
  → chore(release): cut X.Y.Z: bump all three version files, fold both changelogs
  → push/merge to main                                        [cut window opens]
  → addon-build.yml CI validates + builds :sha and :<short-sha> without publishing, proves both images, then publishes and smokes them (NO :X.Y.Z or :latest)
  → push matching v* tag: git tag vX.Y.Z && git push origin vX.Y.Z
  → addon-release.yml pre-flight: tag-ref, semver, config.yaml, manifest.json, pyproject.toml, ha-addon CHANGELOG head, 20-run HA Green evidence, and prebuilt :sha checks
  → addon-release.yml smoke-prebuilt: runs both per-arch :sha images and proves their host-published ports before stable tags exist
  → addon-release.yml promote: publishes :X.Y.Z and :latest from the prebuilt :sha image for amd64 + aarch64
                                                              [cut window closes]
  → addon-release.yml smoke: runs the published amd64 :X.Y.Z image
  → HA discovers new version via config.yaml
  → User clicks "Update" in HA
  → HA pulls image from GHCR
  → Container starts with /run.sh
  → Supervisor materializes /data/options.json; run.sh reads it + /config/secrets.env → sets env vars
  → config.py reads env vars + radio.toml → builds StationConfig
  → main.py starts producer + streamer
```

Every step must succeed. A break at ANY point means the addon doesn't work.

**Important:** The cut merge and the tag push are separate actions. The tag push promotes the already-built `:sha` images to stable tags. Wait for `addon-build.yml` to pass on the cut commit before pushing the tag — `addon-release.yml` fails before publishing if either per-arch `:sha` image is missing.

**The cut window.** Between the cut merge and the second `promote` job, `main` advertises a version whose image is not published yet. A fresh install of the stable add-on fails and rolls back, and an update fails to download. A station already playing keeps playing, because the Supervisor pulls the new image before it stops the old container.

The window is normally under an hour. Leaving it open for longer is how this repo spent 74 of 76 days advertising an uninstallable version (`../release-process.md`). Recovery is under "Cutting a stable release" below: land `git revert <cut-sha>`, the whole cut commit rather than the version files alone, then debug.

To check whether the window is open right now, run `scripts/check-advertised-version.sh`. `advertised-version.yml` runs it daily and raises a flag if it never closed.

## First-listen operator check

Open the add-on Web UI. A fresh unfinished install opens **First Listen** with an
authored 27-second mini-show on deck: an original music bed and a privacy-aware
Marco/Giulia opening, then a source-aware handoff to the live stream. It needs
no AI key or Home context. Source readiness is supporting detail under that
opening; verify that charts, Jamendo, local music, bundled demo music, and
recovery cover are described honestly. The listening cue must distinguish a
primary rotation, recovery cover, and music that still needs repair; bundled
demo music must not be presented as a promised song library.

Required First Listen proof is hearing the station on this device in the add-on
Web UI. Select **Start sound check**, then **Yes, I hear it** only after you hear
the opening, or use the [this-device repair
steps](../troubleshooting.md#first-listen-does-not-play-on-this-device).

Home Assistant speakers remain optional and are no longer part of First Listen.
The add-on has no speaker picker; the route is Home Assistant's own media
browser after installing the HACS integration: **Media → Mamma Mi Radio → Mamma
Mi Radio Live**, or **Developer tools → Actions → Play specified media** against
the speaker with `media-source://mammamiradio/live` and content type `music`.
An accepted Home Assistant service call is not audible proof; confirm the room
yourself. See [Optional: play it on a Home Assistant
speaker](../integrations/ha-integration.md#optional-play-it-on-a-home-assistant-speaker).

First audio does not require an AI key. On a fresh add-on install,
`ha_context_enabled` is omitted and effective Home context stays off. After
audible verification, First Listen offers **Keep Home private** without reading
Home state, or a fresh filtered preview before **Let Marco and Giulia use these
details**. If only generic daylight is available, verify that it is disclosed as
ambient-only and not meaningful personalization, with the private path
recommended. AI-host setup comes later.

If saving the privacy-review receipt fails, verify that the live choice remains
truthful and AI setup stays locked: the private path retries without a preview;
the enabled path requires a fresh preview before saving the review again.

## Version: three files, must match

| File | Field | Example |
|------|-------|---------|
| `ha-addon/mammamiradio/config.yaml` | `version:` | `1.1.0` |
| `pyproject.toml` | `version =` | `"1.1.0"` |
| `custom_components/mammamiradio/manifest.json` | `"version"` | `"1.1.0"` |

CI validates all three match (`scripts/pre-release-check.sh`). If they don't, the build
fails. The HACS integration ships from this same repo and HACS reads its version from the
git release tag, so its `manifest.json` rides the release number too — see
`../release-process.md` → "The HACS integration shares the release number".

**What value they hold.** Between releases: the **last published** version, because the
Supervisor pulls `{image}:{version}` straight from this field and a value with no image
breaks install. They change in exactly one commit — `chore(release): cut X.Y.Z` — and
`scripts/check-advertised-version.sh` is the guard.

**How to bump (only in the cut commit):**
```bash
# All three files, same version, same commit
sed -i '' 's/^version:.*/version: X.Y.Z/' ha-addon/mammamiradio/config.yaml
sed -i '' 's/^version = .*/version = "X.Y.Z"/' pyproject.toml
sed -i '' 's/"version": *"[^"]*"/"version": "X.Y.Z"/' custom_components/mammamiradio/manifest.json
```

## Cutting a stable release (the cadence model)

You develop continuously and never freeze a snapshot — so stable is not "stop and tag
HEAD." It is "promote a build that has already soaked on the edge Pi." The infra is built
for exactly this: `addon-release.yml` does not rebuild on a tag, it promotes the prebuilt
`:sha` image. The edge channel is your continuous soak track.

**`main` advertises the last published version.** Between releases the three version files
name the version currently on GHCR, so the store entry is always installable. The next
number does not exist anywhere yet; the pending *content* accumulates under
`## [Unreleased]`. See `../release-process.md` for why (74 of 76 days uninstallable under
the old rolling-RC model).

**Soaked is a judgment.** "Ready" is your plain read that the edge line you have been
running feels healthy, not a stopwatch on one commit.

**Judge it from the listener's side, not the producer's.** Ask the stream whether it
serves, not the logs whether they are quiet:

```bash
docker exec "addon_${SLUG}" sh -c \
  'curl -s -o /dev/null -w "stream=%{http_code} bytes=%{size_download}\n" --max-time 8 http://127.0.0.1:8000/stream;
   curl -s -o /dev/null -w "healthz=%{http_code} readyz=" --max-time 6 http://127.0.0.1:8000/healthz;
   curl -s -o /dev/null -w "%{http_code}\n" --max-time 6 http://127.0.0.1:8000/readyz'
```

`/stream` must be `200` **and** `bytes` must be non-zero. The status alone is not
enough: a response can open with `200`, send nothing, and sit there until the timeout,
which still reports `stream=200`. Do not judge this one on curl's exit status either.
`/stream` is endless, so `--max-time` always trips it (exit 28) on a perfectly healthy
station. Bytes delivered is the signal.

A soak judged only on producer-side symptoms — dead air,
silence, rescue counts, queue depth — can read perfectly clean while every listener
receives a 500, because the producer is working and the failure is at the response
boundary. That is not hypothetical: a smart apostrophe in the station name made
`/stream` raise on header encoding while `/healthz` stayed `200`, the admin panel
worked, and the watchdog was satisfied. Nothing in the log grep would have shown it.
Prolonged-silence detection cannot catch this either, because listeners fail before
they are ever counted as listeners.

**The cut — 4 steps, when the edge line feels good:**

1. **Land one `chore(release): cut X.Y.Z` PR** via `/ship`:
   - `pyproject.toml`, `ha-addon/mammamiradio/config.yaml`,
     `custom_components/mammamiradio/manifest.json` → `X.Y.Z`
   - **root CHANGELOG**: roll `## [Unreleased]` into a dated `## [X.Y.Z] - <date>`, then
     open a fresh `## [Unreleased]`
   - **ha-addon CHANGELOG**: move its `## Unreleased` content under a real
     `## X.Y.Z - <date>` heading

   Both are REQUIRED. `pre-release-check.sh` §2 compares `config.yaml` against the
   first *versioned* heading in each file, skipping `## Unreleased`. The extractor
   strips brackets, so `## [X.Y.Z]` and `## X.Y.Z` both parse; the root file uses
   brackets and the ha-addon file does not purely as house style, not because a gate
   requires it. `addon-release.yml`'s tag pre-flight checks both again, but that
   fires inside the open window, so a heading typo caught only there costs a revert
   rather than an edit.

   **The cut window opens when this merges.** From here to step 3, `main` advertises a
   version GHCR does not have yet. Capture the SHA now and use it for the rest of the
   cut, because `origin/main` can move underneath you:
   ```bash
   git fetch origin main --tags
   CUT_SHA="$(git rev-parse origin/main)"
   ```
   The cut must already contain the physical 20-run HA Green receipt set from
   the exact clean edge source commit, recorded with the commands in
   [`docs/music-sources.md`](../music-sources.md). Pre-flight fails loud if the
   evidence is missing, stale, or over its two-second p95, or if the tag/version,
   release metadata, changelog head, or either per-arch `:sha` image disagrees.

2. **Wait for `addon-build.yml` green** on `$CUT_SHA` (~15-25 min; the PR touches
   `pyproject.toml` and `ha-addon/**`, both in the build's path filter).

   The long judgment soak belongs *before* the cut, on the edge line. If you want a
   short confirmation that the cut commit itself boots (it differs from its soaked
   parent only by version strings and changelog text), pin edge to it:
   ```bash
   make edge-release ARGS="--target-sha $CUT_SHA"
   ```
   Keep that to hours, not days. The cut window stays open for its whole duration, and
   `advertised-version.yml` will file a drift issue if it is still open at 09:15 UTC.
   That alarm is correct, not a false positive.

3. **Confirm both arch images exist, THEN tag.** Pre-flight checks this too, but
   pre-flight runs *inside* the open window: a missing image there costs a revert, while
   the same check thirty seconds earlier costs a wait.
   ```bash
   ok=1
   for arch in aarch64 amd64; do
     repo="florianhorner/mammamiradio-addon-$arch"
     token="$(curl -fsSL "https://ghcr.io/token?scope=repository:${repo}:pull&service=ghcr.io" \
       | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("token") or "")' 2>/dev/null || true)"
     if [ -z "$token" ]; then echo "$arch: no token"; ok=0; continue; fi
     code="$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $token" \
       -H 'Accept: application/vnd.oci.image.index.v1+json' \
       -H 'Accept: application/vnd.docker.distribution.manifest.list.v2+json' \
       -H 'Accept: application/vnd.oci.image.manifest.v1+json' \
       -H 'Accept: application/vnd.docker.distribution.manifest.v2+json' \
       "https://ghcr.io/v2/${repo}/manifests/${CUT_SHA}")"
     echo "$arch: $code"
     [ "$code" = "200" ] || ok=0
   done
   [ "$ok" = "1" ] || { echo "STOP: image missing, do not tag"; }
   ```
   Both must print `200`; the guard exists so a `404` cannot scroll past unnoticed
   during a cut. Query the **full** `$CUT_SHA`: `addon-build.yml` pushes both a full-SHA
   and a short-SHA tag, but pre-flight validates the full one, so that is the tag worth
   confirming. All four manifest media types are advertised, matching
   `scripts/check-advertised-version.sh` — the registry returns an index today, but a
   single-platform manifest would 406 against a narrower `Accept` and read as a missing
   image. The anonymous token endpoint needs no credentials; an empty parse is treated
   as failure rather than sent as an empty `Bearer`.

   **Tag the cut commit and let CI promote:**
   ```bash
   git tag vX.Y.Z "$CUT_SHA" && git push origin vX.Y.Z
   ```
   Tag `$CUT_SHA` by name, never `origin/main`. If an unrelated commit landed during the
   window, tagging HEAD either fails pre-flight (no `:sha` image for a docs-only commit)
   or, worse, silently publishes a tree you never soaked.

   Pre-flight fails loud if `config.yaml`, `manifest.json`, `pyproject.toml`, or the
   ha-addon CHANGELOG head do not equal the tag, or either arch `:sha` image is missing.

   **The window closes only when both arch `promote` jobs finish** — not at tag push.
   Verify: `docker pull ghcr.io/florianhorner/mammamiradio-addon-aarch64:X.Y.Z`, or just
   `bash scripts/check-advertised-version.sh`.

4. **Write the GitHub Release.** Nothing in CI creates it, and HACS keys the integration
   update off it. There is **no** "open the next RC" step — you are back at steady state.

**If the release fails, revert first, debug second.** Any failure in `addon-release.yml`
leaves the window open indefinitely. Land `git revert <cut-sha>`, then investigate. A stuck
window is a broken install for everyone.

Revert the whole cut commit, not just the version files. The cut also folded both
changelogs, so a version-only revert leaves the ha-addon CHANGELOG head at the unreleased
number: `check-changelog-sync.sh` then refuses the commit locally, and `pre-release-check.sh`
fails the PR in CI. Reverting the commit is atomic across both and passes each gate.

**Never tag the `chore(edge)` metadata commit** — `addon-build.yml` skips those, so it has
no `:sha` image and pre-flight will reject the tag.

**Known limitations (revisit if they bite):**
- `release-cooldown.yml` only fails *red* on a tag <24h after the prior release; it does not
  actually block `addon-release.yml` from promoting. Don't push the tag inside the window (or use
  the `hotfix` label) rather than relying on it to stop you.
- `docker.yml` publishes the standalone image on any `v*` tag even if the addon pre-flight fails.
- The promoted image is built from the cut commit, so it differs from the soaked parent by
  the version strings and changelog text. The bump reaches runtime (the Dockerfile
  pip-installs `pyproject.toml`, so `_ASSET_VERSION` and `bridge_app_version` change).
  Step 2's `--target-sha` soak is what makes "you ran what you tagged" literally true.

## Addon stage

`ha-addon/mammamiradio/config.yaml` declares `stage: stable` for the release channel. The Edge channel stays `stage: experimental` in `ha-addon/mammamiradio-edge/config.yaml` so testers still see the orange Experimental badge on main-branch builds.

## Config options: the contract

When you add an option to the HA addon configuration UI, you must update THREE files in the same commit:

| File | What to add |
|------|-------------|
| `ha-addon/mammamiradio/config.yaml` | Type in `schema:` + an `options:` default only when the field should be visible by default |
| `ha-addon/mammamiradio/rootfs/run.sh` | Key in the Python extraction loop |
| `ha-addon/mammamiradio/translations/en.yaml` | Human-readable name + description |

The `schema:` block drives field order in Home Assistant's Configuration tab. A field may be intentionally omitted from `options:` only when its schema type is optional (`str?`, `password?`, etc.); Home Assistant then hides it behind its "Show unused optional configuration options" disclosure for new installs or installs where the key is absent from saved options. Existing installs that already saved a blank legacy key may still show that key until the saved option is cleared.

CI validates that every `options:` key appears in `schema:` in the same relative order, every schema-only key is optional, and every schema key appears in run.sh. If you add to config.yaml but forget run.sh, the build fails.

Current config options:

| Option | Schema type | Env var |
|--------|-------------|---------|
| `station_name` | `str?` | `STATION_NAME` |
| `enable_home_assistant` | `bool?` | `HA_ENABLED` |
| `ha_context_enabled` | `bool?` | `MAMMAMIRADIO_HA_CONTEXT_ENABLED` (no declared fresh-install default; missing stays omitted/off until the First Listen privacy choice) |
| `ha_context_poll_interval` | `int(1,3600)?` | `MAMMAMIRADIO_HA_CONTEXT_POLL_INTERVAL` (default 300s) |
| `ha_media_player_push` | `bool?` | `MAMMAMIRADIO_HA_MEDIA_PLAYER_PUSH` (on by default; turn off when the HACS integration owns `media_player.mammamiradio`; `run.sh` missing-key fallback true) |
| `quality_profile` | `list(premium\|balanced\|economy)?` | `MAMMAMIRADIO_QUALITY` |
| `admin_token` | `password?` | `ADMIN_TOKEN` (blank => add-on trusts the LAN, no token required) |
| `super_italian_mode` | `bool?` | `MAMMAMIRADIO_SUPER_ITALIAN` |
| `chaos_mode_active` | `bool?` | `MAMMAMIRADIO_CHAOS_MODE` |
| `festival_mode` | `bool?` | `MAMMAMIRADIO_FESTIVAL_MODE` |
| `broadcast_chain` | `bool?` | `MAMMAMIRADIO_BROADCAST_CHAIN` (On-Air Sound; default off — studio-clean, set true to opt into the FM colouring) |
| `guest_host` | `bool?` | `MAMMAMIRADIO_GUEST_HOST` |
| `songs_between_banter` | `int(2,60)?` | `MAMMAMIRADIO_PACING_SONGS_BETWEEN_BANTER` |
| `songs_between_ads` | `int(1,60)?` | `MAMMAMIRADIO_PACING_SONGS_BETWEEN_ADS` |
| `ad_spots_per_break` | `int(1,5)?` | `MAMMAMIRADIO_PACING_AD_SPOTS_PER_BREAK` |
| `norm_cache_mb` | `int(200,8000)?` | `MAMMAMIRADIO_MAX_CACHE_MB` (Music cache size; add-on default 1500, standalone 500. Startup computes an effective limit from available disk space. See `docs/operations.md`, "Music cache sizing".) |

Jamendo is not a Supervisor option. The authenticated **Motore → Setup → Music
sources** flow persists the client ID, enabled intent, current non-commercial
acknowledgement, and acknowledgement revision in owner-only
`/config/secrets.env`. A versioned one-time migration recovers a legacy
Supervisor client ID when possible, but keeps the source disabled until the
operator reviews and acknowledges the current boundary. Additional candidate
tuning can be set in `radio.toml` or container env without exposing Supervisor
UI options: `JAMENDO_COUNTRY`, `JAMENDO_ORDER`, and `JAMENDO_LIMIT` (`1`-`200`).
Add-on local MP3s live at `/data/music`; `run.sh` exports that path as
`MAMMAMIRADIO_MUSIC_DIR` and moves it under the temporary fallback base only
when `/data` is not writable.

**Admin option durability.** Supervisor's stored app options are the sole
durable authority for Super Italian, Chaos, Festival, AI Quality, On-Air Sound,
and pacing. Admin routes commit the selected value through Supervisor before
changing live state: they authenticate to `GET /addons/self/info`, merge the
requested fields into every active-schema option, and send one complete
replacement to `POST /addons/self/options`. `/data/options.json` is a
Supervisor-generated, read-only startup projection; `run.sh` and startup
loaders may read it, but application code must never write it directly or use
it as a persistence fallback.

An upgrade cannot reconstruct a pre-fix selection that existed only in the
running process after Supervisor rematerializes an older stored value. Before
the first fixed Edge update, compare the running admin values with
`ha addons info` and mirror any intentional mismatch through the Home Assistant
Configuration surface.

**Provider secrets.** The five AI/TTS provider credentials live in `/config/secrets.env` in add-on
mode: `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `AZURE_SPEECH_KEY`, `AZURE_SPEECH_REGION`, and
`ELEVENLABS_API_KEY`. Jamendo's four private intent facts live in the same owner-only file:
`JAMENDO_CLIENT_ID`, `MAMMAMIRADIO_JAMENDO_ENABLED`,
`MAMMAMIRADIO_JAMENDO_NONCOMMERCIAL_ACKNOWLEDGED`, and
`MAMMAMIRADIO_JAMENDO_ACK_REVISION`. None is an add-on schema field, so a fresh install never
exposes them to `ha addons info`. Upgraded installs keep their keys through a one-time boot recovery: schema-removed
keys are absent from the Supervisor-generated, read-only `/data/options.json` startup projection, so
`run.sh` fetches the values still held in Supervisor's stored settings via the Supervisor API
(`GET $SUPERVISOR_API/addons/self/info`, token-authenticated, 5s timeout, best-effort) and persists
them into `secrets.env` — every later boot is file-first. The stored copies remain visible to
`ha addons info` until a successful admin mode/pacing save or a later add-on Configuration save
replaces stored settings with only current-schema fields. A Configuration save cannot invoke the
app's credential migration: on an upgraded install, wait for successful startup recovery (or use a
successful admin mode/pacing save) before changing Configuration. Jamendo uses its own versioned
migration marker and remains disabled after migration. `ADMIN_TOKEN` remains a Supervisor option.
`/config/secrets.env` is plaintext in the add-on config
storage, not Home Assistant `/config/secrets.yaml`; anyone with host/add-on config access can read it.

Setup treats AI-host and premium-voice readiness separately. Anthropic or
OpenAI completes the AI-host step; OpenAI can also supply voices. Azure Speech
and ElevenLabs are voice-only and do not unlock generated host writing. Azure
is ready only with both its key and region; a partial pair is shown as
incomplete.

`secrets.env` grammar is intentionally small: `KEY=VALUE` lines, optional `export KEY=VALUE`,
whitespace around keys or values, single or double quoted values, values containing `=`, UTF-8 BOM,
and CRLF endings are accepted. Full-line comments beginning with `#` are ignored. Inline comments are
not special for unquoted values, so `OPENAI_API_KEY=sk#abc` means the value contains `#abc`.

**AI quality / model selection.** `quality_profile` (premium | balanced | economy)
replaced the old `claude_model` dropdown. The operator picks *intent*, not a model
snapshot, and `run.sh` maps it to `MAMMAMIRADIO_QUALITY` (a missing/blank value
defaults to `balanced`). Creative work uses Opus/large in `premium`, Sonnet/small
in `balanced`, and Haiku/small in `economy`; latency-sensitive `fast` work stays
on Haiku/small in every profile. If the Supervisor-generated, read-only
`/data/options.json` startup projection still contains the removed
`claude_model` key, `run.sh` also
exports it as the legacy `CLAUDE_MODEL` fast-role override while no
`quality_profile` exists. A successful Supervisor-backed admin save removes the
legacy field once `quality_profile` is present. The canonical model IDs, OpenAI
TTS selection, and script-token prices live in the root `model_registry.toml`
(see "Dynamic LLM routing" in the root `CLAUDE.md`).
**To add or swap a model:** update the relevant registry catalog entry and its
matching `[pricing.catalog.<provider>]` key in the same change—no code or schema
change. The add-on image copies this canonical root file; do not create an
add-on-specific registry copy. An unknown experimental `--models` candidate in
the evaluator uses the registry's conservative fallback price and is marked
unpriced in its JSONL output.

The option extraction in `run.sh` uses one guarded Python script. It reads
Supervisor-owned product options from `/data/options.json`, overlays the
supported owner-only AI/TTS and Jamendo facts from `/config/secrets.env`, and
forces `MAMMAMIRADIO_ALLOW_YTDLP=false`. Behavior toggles use explicit mappings
(`enable_home_assistant` → `HA_ENABLED`, `ha_context_enabled` →
`MAMMAMIRADIO_HA_CONTEXT_ENABLED`, `ha_context_poll_interval` →
`MAMMAMIRADIO_HA_CONTEXT_POLL_INTERVAL`, `super_italian_mode` →
`MAMMAMIRADIO_SUPER_ITALIAN`, `chaos_mode_active` →
`MAMMAMIRADIO_CHAOS_MODE`, `festival_mode` →
`MAMMAMIRADIO_FESTIVAL_MODE`, `broadcast_chain` →
`MAMMAMIRADIO_BROADCAST_CHAIN`, `ha_media_player_push` →
`MAMMAMIRADIO_HA_MEDIA_PLAYER_PUSH`, `guest_host` →
`MAMMAMIRADIO_GUEST_HOST`, `quality_profile` → `MAMMAMIRADIO_QUALITY`
defaulting to `balanced`). A missing `ha_context_enabled` is deliberately not
exported, preserving the fresh-install privacy gate. Pacing options export only
when an integer value is
present (`songs_between_banter` →
`MAMMAMIRADIO_PACING_SONGS_BETWEEN_BANTER`, `songs_between_ads` →
`MAMMAMIRADIO_PACING_SONGS_BETWEEN_ADS`, `ad_spots_per_break` →
`MAMMAMIRADIO_PACING_AD_SPOTS_PER_BREAK`); malformed values are skipped so one
bad key cannot drop every export. To add a new non-provider option:

1. Add to `schema:` in `config.yaml`; also add to `options:` in the same relative order only if it should be visible by default
2. Add a translation entry in `translations/en.yaml`
3. Add the run.sh export, either in the tuple loop for direct UPPER_CASE keys or as an explicit mapping for app-specific env vars
4. Read it in `config.py` via `os.getenv("MY_OPTION", "default")`
5. Add a row to the **Current config options** table above. The table must include every option.
6. Mirror steps 1-2 into `ha-addon/mammamiradio-edge/`. `scripts/validate-addon.sh` checks stable and Edge parity.

**Media-player ownership.** Stable and Edge manifests default
`ha_media_player_push` to `true`, so an add-on-only setup gets a basic
`media_player.mammamiradio` tile out of the box. When the operator installs the
HACS integration (which registers a controllable `media_player.mammamiradio`),
they turn this option off so the two don't fight over the id; the integration
raises a Repair if it detects the lingering REST ghost. Keep the `run.sh`
missing-key fallback at `true` so installs that never saved the option still get
the tile.

## Secrets: password type

Secrets that remain in the Supervisor schema use `password` type (not `str`). This masks them in the HA UI. The AI/TTS provider fields were removed from the schema entirely — provider credentials belong in `/config/secrets.env`, never in Supervisor options.

```yaml
schema:
  my_api_key: password?
```

## Dockerfile: local source, not GitHub

The addon Dockerfile installs mammamiradio from LOCAL source copied by CI into the build context. It does NOT fetch from GitHub. This means:

- The image always matches the exact commit that triggered the build
- No dependency on GitHub being reachable during Docker build
- No risk of building with stale code from a different branch

CI copies `mammamiradio/`, `pyproject.toml`, `radio.toml`, and the root
`model_registry.toml` into `ha-addon/mammamiradio/` before building.
The checked-in `ha-addon/mammamiradio/radio.toml` must remain byte-for-byte identical to the root `radio.toml`; local validation and CI now fail if those files drift.
Unlike `radio.toml`, `model_registry.toml` must NOT be committed under `ha-addon/mammamiradio/` at all — it is staged from the root file only at build time, and `validate-addon.sh` fails if a committed copy is found.

Before every commit or push that touches addon packaging, run:

```bash
scripts/validate-addon.sh
```

That command checks the same add-on invariants CI validates. Add `--build` when you also want the slower local-source image build. If this command fails, do not push.

## `io.hass.*` image labels

The addon Dockerfile must declare three Home Assistant image labels using `ARG`-injected build arguments:

```dockerfile
ARG BUILD_VERSION
ARG BUILD_ARCH
LABEL \
  io.hass.version="${BUILD_VERSION}" \
  io.hass.type="app" \
  io.hass.arch="${BUILD_ARCH}"
```

The HA Supervisor reads these labels to:
- `io.hass.version` — Supervisor does not read this for add-ons. `DockerAddon.version` overrides `DockerInterface.version` and returns the add-on's `config.yaml` version, so update decisions never consult the label; the label-reading path serves Home Assistant Core, the Supervisor, and plugins. Keep declaring it, since the HA docs require it and `validate-addon.sh` checks the declaration is present, but read update behaviour from `config.yaml` `version:` alone. (Corrected 2026-08-02. This runbook previously said the opposite.)
- `io.hass.type` — identify this as an application add-on (as opposed to a system add-on).
- `io.hass.arch` — validate that the pulled image targets the correct host architecture.

CI injects the values via `--build-arg` in `addon-build.yml`. The one build per arch
sets `BUILD_VERSION` = stable `config.yaml` version (`X.Y.Z`), `BUILD_ARCH` = matrix arch,
and tags the image `:${git_sha}` (full) plus `:<short-sha>` — the latter is the tag the
edge channel points at.

`scripts/validate-addon.sh` check 11 verifies that all three label strings are present in the Dockerfile and exits non-zero if any are missing. `ARG BUILD_VERSION=unknown` provides a default so local Docker builds that omit `--build-arg BUILD_VERSION` produce `io.hass.version=unknown` rather than an empty string.

## Image path

HA expects images at:
```
ghcr.io/florianhorner/mammamiradio-addon-{arch}
```

This is set in `ha-addon/mammamiradio/config.yaml` (`image:` field) and must match what `addon-build.yml` pushes to. CI validates this.

The standalone Docker image (for non-HA users) is separate: `ghcr.io/florianhorner/mammamiradio`. Built by `docker.yml` on version tags only.

## Release channels

Stable add-on images are published by `addon-release.yml`, triggered by a `v*` tag push to the version-bump commit after it merges to `main`. GitHub Releases are curated standalone announcements; always write release notes rather than copying raw `CHANGELOG.md`. Tag the version-bump commit — not a later one — so the release image matches the commit CI already validated.

`addon-release.yml` does not rebuild the add-on. It first validates at least 20
physical Home Assistant Green cold-launch receipts bound to the tested source
commit, requires nearest-rank first-byte p95 at or below two seconds, and proves
that the tagged commit changed nothing after that source except the receipt JSON
files. It then verifies that both per-arch `:${git_sha}` images exist, runs the
launch and host-published-port proofs for each native architecture before stable
publishing, and promotes those exact
images to `:X.Y.Z` without changing the source manifest shape, updates `:latest`
only when the current tag is the newest stable semver, and then smoke-tests the
published amd64 `:X.Y.Z` image. The source `:sha` image is built with
`io.hass.version` set to the stable `config.yaml` version; between releases most
`:sha` images therefore carry the last published number. That is inert because
Supervisor reads `config.yaml`, not the label. If a previous run published one architecture
and then failed, a rerun is allowed only when the existing `:X.Y.Z` tag digest
matches the source `:sha`; mismatched stable tags fail and must be cleaned up
manually. The standalone version-tag workflow runs the same HA Green receipt
validator before its first registry write.

## Edge channel (dev releases)

`mammamiradio-edge` is a second add-on in this same repo (`ha-addon/mammamiradio-edge/`) for soak-testing `main` on real hardware without disturbing stable users.

| | Stable (`mammamiradio`) | Edge (`mammamiradio-edge`) |
|--|--|--|
| `version:` | hand-bumped `X.Y.Z` on deliberate releases | the short SHA of the newest `main` commit with a built image (may trail HEAD), cut with `make edge-release` |
| Updates when | you push a matching `v*` tag after merging the version-bump commit | you cut an edge release (the version string changes, so HA shows an Update) |
| Image tag pulled | `:X.Y.Z` (published by `addon-release.yml`) | `:<short-sha>` (published by `addon-build.yml` on every `main` build) |
| Audience | everyone | the maintainer's soak Pi |

Both add-ons pull the **same image repo** (`ghcr.io/florianhorner/mammamiradio-addon-{arch}`) — they just resolve to different tags. The edge folder holds only metadata (`config.yaml`, `translations/`, `CHANGELOG.md`, icons); it has no `Dockerfile` because HA pulls the prebuilt image.

**Cutting an edge release.** Edge releases are **manual and deliberate** — there is no CI bot. The HA Supervisor pulls `{image}:{version}` (the `version:` field *is* the Docker tag) and decides "update available" by a version-string compare, so advancing the edge `version:` to a new value surfaces an in-place Update on the soak Pi. To cut one:

1. Run `make edge-release` (`scripts/cut-edge-release.sh`). It selects the **newest `main` commit with a green `Build HA Addon` run** (that success is the proof both per-arch `:<short-sha>` images were pushed), validates the release-beat manifest against that target SHA (`scripts/validate-release-beat.py --channel edge --target-sha "$SHA"` — a no-op if the manifest is absent/disabled), sets the edge `version:` to that commit's short SHA, and opens a normal PR you merge via `/ship`. You no longer pre-check the build by hand — the script does it via `gh run list`.

The pin **may trail `origin/main` HEAD**: when the tip commits touch only files outside the complete image trigger set (`ha-addon/**`, `mammamiradio/**`, `pyproject.toml`, `radio.toml`, `model_registry.toml`, `scripts/validate-addon.sh`, `scripts/ha-green-launch-smoke.py`, `scripts/ha-green-perf-smoke.py`, and `.github/workflows/addon-build.yml`), `Build HA Addon` never ran for them and no `:<sha>` image exists, so pinning HEAD would make the Supervisor pull a missing tag. The script pins the last *built* commit instead, and **hard-fails (no PR)** rather than warn-and-continue when it cannot find a successful build run, when `gh` cannot be queried, or when an image file changed between the built commit and HEAD (which means the newest image-affecting commit has not gone green yet — wait for it, or fix the failed build). `scripts/cut-edge-release.sh` mirrors this trigger set exactly, and its hermetic test fails on drift. It uses `gh run list` (needs only `actions:read`); it no longer calls the GHCR packages API (which needed the `read:packages` scope the maintainer token lacks and 403'd into a soft-pass).

Because *you* open the PR (not a bot / `GITHUB_TOKEN`), its required checks (`quality`, `pi-smoke`) run normally and you merge it like any PR — no protected-branch fight, no self-merging CI, no races. Stable is never touched. (This replaced an auto-bump CI job that opened a PR and busy-waited on its own checks; it raced check-creation and orphaned PRs — see #384 / #476 / #487.)

**Constraint:** `Build HA Addon` is push-only (it does not run on PRs), so it must never be a required check on `main` — requiring it would make every PR unmergeable.

**Smoke runs in addon mode.** Every smoke `docker run` (`addon-build.yml`, and both blocks in `addon-release.yml`) sets `-e SUPERVISOR_TOKEN=smoke-ci`, mirroring how the HA Supervisor launches the image. Without it the container boots in standalone mode, where binding `0.0.0.0` with no admin token is a fatal config error (`config._is_addon` is false), uvicorn never starts, and the smoke fails with `/healthz` connection-refused — a false negative that doesn't reflect the real addon. Keep the token on any new smoke step.

For both `amd64` and `aarch64`, `addon-build.yml` starts the exact SHA image with
`127.0.0.1:8765:8000` published and probes `/healthz` through that host port.
Before stable promotion, `addon-release.yml` repeats that published-port proof
for both exact SHA images on native-architecture runners. This is separate from
the in-container launch smoke: it proves that each image actually exposes the
port Home Assistant connects to. The launch smoke's warm and cold scenarios
first give the fresh process a separate readiness budget, then require an
accepted stream byte within two seconds of the listener's `/stream` request and
agreement across `/healthz`, `/readyz`, and `/public-status`. This is a
fresh-process listener-to-first-byte contract, not a process-spawn-to-audio
measurement. A cold start must open on approved packaged recovery speech; the
technical emergency tone is a last-resort continuity rung and does not count as
a healthy cold open.

**Switching the soak Pi to edge.** Edge and stable both use `host_network: true` and port 8000 — they cannot run at the same time. Uninstall stable, install "Mamma Mi Radio (Edge)" from the same Apps catalog entry, re-enter API keys. Reverse it to go back.

**Editing the edge add-on.** Its `options`/`schema` MUST stay identical to stable — edge runs the same image and the same `run.sh` reads the options. `scripts/validate-addon.sh` fails CI on any drift. When you add a config option to stable (the THREE-files contract above), the edge `config.yaml` and `translations/en.yaml` are a fourth and fifth file to update in the same commit. The edge `version:` line is the only field that changes to cut a release, and `make edge-release` does that for you.

## Hot backup and restore contract

Stable and Edge declare the same `backup: hot` contract. Home Assistant can
therefore collect the app's `/data` and `/config` trees while playback keeps
running; there are no stop/start backup hooks. The exclusion list keeps
Supervisor out of high-churn, rebuildable audio:

- `/data/tmp`, yt-dlp scratch, restart-handoff media, and temporary share clips
- direct generated audio and sidecars under `/data/cache`
- `.part`, `.ytdl`, and `.tmp` files at any depth

The backup retains the Supervisor-generated `/data/options.json` startup
projection, `/config/secrets.env`, `/data/cache/mammamiradio.db`, durable JSON
state and flags, and provenance ledger `.jsonl` / `.jsonl.gz` files. Supervisor's
stored app options—not the projection—remain the durable settings authority;
the other retained files hold provider keys, station memory, and history.
Generated downloads, normalization outputs, renders, and clips warm again after
restore.

`/data/music` is the add-on's operator-managed local music library: `run.sh`
exports it as `MAMMAMIRADIO_MUSIC_DIR`, and the app resolves local MP3s from
that path (moving under the temporary fallback base only when `/data` is not
writable). Backing it up restores the local library along with the rest of the
retained state.

This is a live, file-level copy, **not a copy taken from one single exact
moment** of the retained state. SQLite may commit while Supervisor is
traversing the tree; its rollback journal remains included when present, but
inclusion alone does not make the copy a single-moment snapshot. Ledger
rollover writes a
`.jsonl.gz.tmp`, atomically publishes the `.jsonl.gz`, then removes the source
`.jsonl`, while retention can delete old ledger files. Excluding `*.tmp`
protects the staging file, but a narrow source-delete or retention-delete race
remains. The contract removes the observed high-churn generated-media race; it
does not promise universal hot-snapshot consistency.

**When the manifest takes effect.** Merging the manifest changes updates the
repository metadata seen by a new or reinstalled Stable app after its catalog
refresh. An existing Stable installation keeps the manifest metadata that came
with its installed app version until it updates. Edge is the controlled
installed canary. A merge by itself does not change a running installation.

### Edge backup canary

**Trigger: a change to the backup contract, not every release.** The contract is
the `backup: hot` declaration and its exclusion list.
`tests/addon/test_addon_backup_contract.py` pins both on every PR — that Stable
and Edge match exactly, that generated and incomplete artifacts are excluded,
and that durable state is retained. When that test passes and the contract did
not change, a manual re-check of the same properties proves nothing new.

What the test cannot observe is the part worth running by hand: Supervisor takes
a **live file-level copy of a running SQLite database and rotating ledgers**,
with no stop hooks. That race does not vary by release, but it does re-open
whenever the exclusion list or the retained set moves. Run the canary then.

Run it after the exact-head Edge image is built and Edge is installed through
the normal planned update path. That update has one expected restart; the backup
itself must not restart the app.

1. Start a partial backup while audio is playing. Require Edge to be present,
   `failed_addons` to be empty, and no
   `Error adding ... No such file or directory` message in the backup log.
   `/healthz` and `/readyz` must remain `200`, the restart count must not change
   during the backup, and listening must remain continuous.
2. On the extracted `mammamiradio.db`, require both
   `PRAGMA quick_check;` and `PRAGMA integrity_check;` to return `ok`. Confirm
   the expected tables are present: `tracks`, `play_history`,
   `listener_persona`, `listener_session_receipts`, `track_rules`, `song_cues`,
   and the install-origin witness
   `_mammamiradio_home_install_origin_v1`.
3. Validate every plain ledger JSONL row, run a gzip integrity check on each
   `.jsonl.gz`, then parse every decompressed row as JSON. An archive that
   merely opens is not sufficient: rotation can publish a `.jsonl.gz` and delete
   its source while Supervisor walks the tree.

SQLite integrity and ledger readability are the gate. If either fails, or a
retained ledger file hits the narrow delete race, stop the rollout and design an
application-coordinated snapshot. Do not switch to cold backup or add emergency
stop/start hooks as a workaround.

**Restore into a disposable Home Assistant installation** verifies the other
half — that a valid archive actually brings a station back up, settings and
provider keys and station memory intact, generated audio rebuilding. Run it when
a disposable instance is available. It needs Supervisor, so the plain container
image cannot stand in. Absent that instance, the round trip is unverified and
the release carries that gap knowingly; record it in
`../stabilization-log.md` rather than letting it pass silently.

**Retired from this gate, and why.** Archive member inspection and the
`secrets.env` digest comparison were per-release steps until 2026-08-04. Both
re-ask what `test_addon_backup_contract.py` now answers on every PR. Confirming
the next scheduled automatic backup is monitoring, not a release gate. Running
all three every cut trained the gate to be skipped wholesale for convenience
rather than pared to what only a live host can show.

## Landing a PR (merge gate)

Landing is mechanized — see the **Landing contract** in `CLAUDE.md` "Quality
gates" (single source of truth). The short version:

- `/ship` opens the PR and never arms auto-merge; the PR soaks (CodeRabbit,
  review time) until Florian gives the merge signal.
- On the signal, run `scripts/land-pr.sh <PR#>`. It verifies the pre-ship
  squad entry against the PR head (code-state freshness — a soak of days is
  fine, a push after the review is not), updates the branch if it is behind
  (CI re-runs on the integrated state), and arms
  `gh pr merge --squash --auto --match-head-commit <head>` so the merge only
  fires on the exact head it verified.
- Raw `gh pr merge` and mutating `gh api` merge calls are denied by the local
  hook (`scripts/hooks/require-preship-squad.sh`); `--disable-auto`
  (disarming) is allowed. The hook is a local guard, not a security boundary.
- Branch protection on `main` has strict status checks (branch must be up to
  date before merging) since 2026-06-12. There is deliberately no workflow that
  posts Dependabot rebase or recreate commands. The retired nudge used a
  `GITHUB_TOKEN` actor that Dependabot rejected for lacking push access, and
  batch-wide retries caused repeated comments and CI churn after each merge.
  Dependabot auto-merge remains opportunistic: a behind PR parks until an
  authenticated maintainer handles that specific PR. If Dependabot still owns
  the branch, request its rebase as the maintainer; if the branch was edited,
  use `@dependabot recreate` and re-review the new head. Human-authored PRs land
  through `scripts/land-pr.sh <PR#>`, which updates the branch after verifying
  pre-ship evidence.
- Settings drift tripwire: `bash scripts/check-merge-gate.sh` (also part of
  `make pre-release`) asserts strict checks, `allow_update_branch`,
  `allow_auto_merge`, and the required contexts. Run it if landing behaves
  oddly.

## Pre-merge checklist

Before merging ANY change that touches addon files:

- [ ] `scripts/validate-addon.sh` passes locally
- [ ] Version bumped in all three files (if this is a release)
- [ ] `ruff check . && ruff format --check .` passes
- [ ] `pytest tests/` passes (200+ tests)
- [ ] `make media-check` passes; a release also has complete `make media-proof`
      output and the 20-run Home Assistant Green cold-listen receipt
- [ ] If new config option: added to config.yaml + run.sh + translations
- [ ] If path changed: grep all files for the old path
- [ ] If renamed anything: `grep -r "old_name" .` returns zero hits
- [ ] Landing goes through `scripts/land-pr.sh` (see "Landing a PR" above) —
      `scripts/check-merge-gate.sh` passes if anything about merging looks off

**After merging a cut commit**, follow "Cutting a stable release" above. Do not tag `HEAD`: tag the cut commit itself, and if the release workflow fails, land `git revert <cut-sha>` — the whole cut commit, not the version files alone.

## Release invariants gate (2026-04-27 onward)

`scripts/check-release-invariants.sh` runs on every PR via `quality.yml`. It catches audio delivery invariants that have caused production silence incidents, plus a release-beat manifest check:

1. **FFmpeg `music_eq_chain` eq count**: must be exactly 2. A 3rd `equalizer=` filter in `mammamiradio/audio/normalizer.py` triggers FFmpeg 8.x SIGABRT on Pi aarch64. Local: `bash scripts/check-release-invariants.sh`.
2. **Packaged recovery audio**: both `continuity_1.mp3` and
   `emergency_tone.mp3` must ship under
   `mammamiradio/assets/demo/recovery/`, exceed 1 KiB, match their approved
   `spoken_assets.json` hashes, and contain an FFprobe-readable audio stream.
   `producer.py` must not call `generate_silence` in recovery paths.
3. **`_pick_canned_clip=None` test mock**: at least one test file must mock this to `None`. Tests that return a real file hide the empty-container / missing-packaged-clip scenario that can happen in a broken image.
4. **`session_stopped` test**: at least one test file must reference `session_stopped`. Covers the post-restart scenario where the HA watchdog restarts the addon with the flag still set.
5. **HA Green fallback performance gates**: `QUEUE_FALLBACK_WAIT_SECONDS` stays <= 5s, the norm-cache rescue avoids deterministic first-file selection, and the HA Green perf/launch smoke scripts + Make targets exist. The perf smoke skips its stream-byte probe only for a persisted operator stop confirmed independently by `503 stopped` from `/readyz` and `session_stopped: true` from `/public-status`; every other starting or ready state must still produce bytes.
6. **Starter media proof**: `make media-check` validates the canonical manifest,
   evidence, bytes, and audio quickly. `make media-proof` additionally proves
   wheel/sdist and amd64/aarch64 image parity, FFprobe facts, add-on extractor
   absence, and Jamendo transience. While the starter content is absent by
   design, the PR quality lane's direct step, the release-invariants media
   section, the add-on build validate job, the add-on build full media-proof
   job (so the proof remains visible while image publish and the edge channel
   keep flowing), the edge cut, and
   local `make media-check` run their proof report-only (verdict plus a
   missing-content notice, exit 0); the stable promotion media-proof job in
   `addon-release.yml` and `scripts/pre-release-check.sh` section 10 keep the
   hard gate on the release path. Stable remains blocked until exactly 12
   approved derivatives total at least 45 minutes and no more than 75 MiB, every
   full audition receipt is complete, and 20 cold HA Green runs show p95 first
   accepted non-silent starter byte at or below two seconds.
7. **Release beat source manifest**: `scripts/validate-release-beat.py` (no args) checks that `mammamiradio/assets/release/release_beat.toml`, if present and enabled, has valid schema, listener-safe copy, and is declared in `pyproject.toml` package-data. A missing or explicitly disabled manifest passes as a no-op.

**Version sync check**: also wired into every PR. If `pyproject.toml` or `ha-addon/mammamiradio/config.yaml` appears in the PR diff, CI runs the full `scripts/pre-release-check.sh` (version consistency + CHANGELOG head + all invariants). No-ops on non-version PRs. This closes the version-drift class of bug that caused the stale 2.10.7→2.10.9 CHANGELOG incident.

Local pre-release: `make pre-release` (runs the full eight-check
`pre-release-check.sh`, including independent validation of both packaged
recovery assets and a target-scoped release-beat check:
`--channel stable --semver "$ADDON_VER"`, which additionally confirms the
manifest's channel/semver match the release being cut when the manifest is
enabled).

## Release cooldown (stabilization run, 2026-04-17 onward)

A 24-hour minimum gap is enforced between consecutive published releases. The gate is `.github/workflows/release-cooldown.yml`; it runs on every `v*` tag push and queries GitHub Releases for the prior published (non-draft, non-prerelease) release's `publishedAt`.

- Block rule: `prior_release_time + 24h > now` => status check fails, release surfaces red.
- Bypass: the PR that introduced the tagged commit carries the `hotfix` label. The workflow skips the cooldown check entirely. Intended for P0/P1 regressions the existing release just introduced.
- Override: `MIN_COOLDOWN_HOURS=<n>` at workflow level (not set by default) tightens or relaxes the window.
- Self-test: `bash tests/workflows/test_cooldown_gate.sh` runs 9 scenarios (1h / 24h boundary / 25h / MIN_COOLDOWN_HOURS override / malformed ISO / clock skew / no-prior). Wired into `quality.yml` — runs on PRs that touch `.github/`, `scripts/`, or `tests/workflows/`, and on every push to `main`.

**Trust model:** the `hotfix` label is not access-controlled beyond the repo's default label permissions. Anyone with triage rights can apply it. Acceptable for the current single-maintainer team; revisit if PR volume grows. Day 8 Go/No-Go uses `../stabilization-log.md` to evaluate whether the gate is working.

## Post-merge verification

After merging to main, verify the full chain:

1. **CI passed**: Check GitHub Actions for green build
2. **Image exists on GHCR**: `docker pull ghcr.io/florianhorner/mammamiradio-addon-aarch64:VERSION`
3. **Image is public**: Check github.com/florianhorner?tab=packages
4. **HA sees update**: Settings > Apps > Mamma Mi Radio > shows new version
5. **Update works**: Click Update, wait for download, check logs
6. **App starts**: Addon log shows "Starting uvicorn on 0.0.0.0:8000..."
7. **Ingress works**: Click addon in sidebar, dashboard loads

Do NOT merge the next PR until all 7 steps pass.

## Operator Stop and assetless Resume

Stop persists the operator pause across add-on or watchdog restarts. A normal
Resume remains fail-closed until readable recovery audio is available. If every
recovery asset is missing, it returns HTTP `503` with
`force_available: true`, keeps the station stopped, and the Admin UI asks for
explicit confirmation.

Only that confirmation sends `POST /api/resume?force=true`. The confirmed
escape removes the stop marker, requests a host break, returns
`{"ok":true,"recovering":true,"runway_source":"none"}`, and leaves `/readyz`
at `503 starting` until a listener actually accepts the rebuilt audio. Force
Start is recovery for a corrupt installation; it is never automatic.

## Expected log signatures after a release

Use these to tell intentional degradation from a real regression during post-merge verification and soak runs.

**Healthy startup**: boot summary line, one `Producing MUSIC:` within a few seconds, no repeated `queue empty` warnings.

**Anthropic auth suspended (intentional)**: one `Anthropic auth failed — suspending for 10 minutes` followed by OpenAI script generation. If you see this line repeating every few seconds, the WS3-A cooldown broke.

**TTS voice substituted (intentional)**: one `Invalid voice 'X' for backend edge; falling back to it-IT-DiegoNeural` at boot. Zero per-segment `Invalid voice` lines. Dashboard shows `tts_degraded` badge.

**Starter catalog admitted (required)**: the boot summary identifies the
attributed starter/local base and the first `Producing MUSIC:` line follows
without an external provider download. A manifest, hash, evidence, or audio
validation error is a release/image regression, not expected degradation.

**Jamendo unavailable (optional)**: The provider logs
`Jamendo provider preparation started`, followed by `Jamendo provider ready`,
when preparation succeeds. Retryable failures log
`Jamendo provider attempt failed` with only a coarse `failure_code`, a bounded
numeric `provider_code` (or `none`), and `retry_in_seconds`. Blocked attempts
log `Jamendo provider blocked` with a coarse `failure_code` and bounded numeric
`provider_code`. Configuration apply, queue cleanup, and manual retry failures
log `Jamendo provider control failed` with only a `failure_code` set to
`config_apply_failed`, `queue_cleanup_failed`, or `retry_failed`.

`failure_code=api_failed provider_code=3` means Jamendo rejected the request
format or one of its parameters, so the provider enters the blocked state.
Invalid or suspended client IDs also block. Rate limits remain retryable.
Starter and local music continue while Jamendo is degraded or blocked. Status
and logs omit the client ID, private audio URL, raw response text, and raw
provider exception. The provider removes its single-use artifact after
cancellation, failure, playback, or restart.

**Session track denylist (intentional)**: `WARNING Skipping music track due to
invalid audio (…): …` plus `WARNING Purged rejected cache artifact …` when an
eligible local/standalone artifact fails validation. If every source key is
denied, the producer queues its normal protected recovery ladder instead of
retrying the same sources.

**Queue starvation rescue (intentional)**: protected packaged recovery, admitted
non-transient music, a bounded recovery sweeper, or the emergency tone may bridge
the base queue. Jamendo never appears in norm cache, rescue, or restart handoff.
A forced-banter `force_next = BANTER` remains the last-resort escape.

**Regression signatures** (these indicate a real problem, not intended behaviour):

- Repeated `Invalid voice '…' for backend …` on every segment
- Repeated `Anthropic auth failed` more than once per ~10 minutes
- `music audio too short (…)` on the same track more than once per session
- `/readyz` staying at `503 starting` for more than 90 seconds with listeners connected

## Common failures

### "An unknown error occurred with addon"
- Check app logs (**Settings > Apps > Mamma Mi Radio > Log**)
- If "radio.toml not found": image is corrupt, rebuild
- If "model not found": the provider's registry catalog value does not match its
  API (the circuit breaker falls back automatically, but fix the canonical
  `model_registry.toml` catalog entry)
- If Python traceback: the code has a bug, check the specific error

### Image shows as "private" on GHCR
- Go to github.com/florianhorner?tab=packages
- Click the package > Package settings > Change visibility > Public
- This only needs to be done once per new package name

### "Not a valid add-on repository"
- `repository.yaml` must be on `main` branch (not a feature branch)
- The repo URL in HA must be `https://github.com/florianhorner/mammamiradio`

### Version shows but update or install fails

This is the cut window left open, not a user error. `main` is advertising a version whose
image was never published, so the store entry looks healthy (the Supervisor never contacts
the registry when reading the store) and fails only on click. A fresh install fails and
rolls back; an update fails to download but leaves a playing station alone.

- Diagnose: `bash scripts/check-advertised-version.sh`
- Or by hand: `docker pull ghcr.io/florianhorner/mammamiradio-addon-aarch64:VERSION`
- **Release mid-flight?** Wait for `addon-release.yml` to finish promoting *both*
  architectures, then re-check.
- **Release failed or abandoned?** Land `git revert <cut-sha>` immediately, then debug.
  Revert the commit rather than the version files alone: the cut folded both changelogs
  too, and a partial revert is refused by `check-changelog-sync.sh` and `pre-release-check.sh`.
- `advertised-version.yml` raises a flag daily if this state persists.

## Hardcoded values that must stay in sync

| Value | Files |
|-------|-------|
| Port 8000 | config.yaml (`ingress_port`), run.sh (`MAMMAMIRADIO_PORT`, `--port`), config.py (default) |
| `MAMMAMIRADIO_ALLOW_YTDLP=false` | run.sh (hardcoded; both add-ons omit external extraction authority) |
| `MAMMAMIRADIO_LEDGER_ENABLED=true` | run.sh (hardcoded, enables per-segment provenance ledger in the addon; data stays local at `/data/cache/ledger/`) |

If you change any of these, grep for the old value and update all locations.
