# HA Addon Release Runbook

How to release a new version of the Mamma Mi Radio Home Assistant addon without breaking anything.

## The release chain

```
Code change
  → bump version in all three files (see below)
  → push/merge to main
  → addon-build.yml CI validates + builds :sha and :<short-sha> (NO :X.Y.Z or :latest)
  → push matching v* tag: git tag vX.Y.Z && git push origin vX.Y.Z
  → addon-release.yml pre-flight: tag-ref, semver, config.yaml, manifest.json, and prebuilt :sha checks
  → addon-release.yml smoke-prebuilt: runs the amd64 :sha image before stable tags exist
  → addon-release.yml promote: publishes :X.Y.Z and :latest from the prebuilt :sha image for amd64 + aarch64
  → addon-release.yml smoke: runs the published amd64 :X.Y.Z image
  → HA discovers new version via config.yaml
  → User clicks "Update" in HA
  → HA pulls image from GHCR
  → Container starts with /run.sh
  → run.sh reads /data/options.json + /config/secrets.env → sets env vars
  → config.py reads env vars + radio.toml → builds StationConfig
  → main.py starts producer + streamer
```

Every step must succeed. A break at ANY point means the addon doesn't work.

**Important:** The version-bump merge and the tag push are separate actions. The tag push promotes the already-built `:sha` images to stable tags. Wait for `addon-build.yml` to pass on the version-bump commit before pushing the tag — `addon-release.yml` fails before publishing if either per-arch `:sha` image is missing.

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

**How to bump:**
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

**Rolling release candidate.** `main`'s stable `config.yaml` / `pyproject.toml` always carry
the *next* version (the RC). Every `main` commit bakes that version into its `:sha` image. The
number is not "where I am" — it is "what I release next." A stable tag that lags `main` by many
commits is *correct*, not outdated.

**Promote-current-edge.** The release candidate is always whatever your current edge release
points at — already built, already on your Pi. No SHA archaeology. "Soaked" is your plain
judgment that the line you have been running has felt healthy, not a stopwatch on one commit.

**The cut — 3 steps, when the edge line feels good:**

1. **Tag the current edge SHA** (not HEAD, not the `chore(edge)` metadata commit — that commit
   has no `:sha` image because `addon-build.yml` skips it). The candidate is whatever
   `ha-addon/mammamiradio-edge/config.yaml` `version:` names:
   ```bash
   git fetch origin main --tags
   EDGE=$(git show origin/main:ha-addon/mammamiradio-edge/config.yaml | awk '/^version:/{print $2}')
   # X.Y.Z must equal the STABLE config.yaml version at $EDGE
   git tag vX.Y.Z "$EDGE" && git push origin vX.Y.Z
   ```
   `addon-release.yml` pre-flight fails loud if `config.yaml` or `manifest.json` != tag,
   or either arch `:sha` image is missing — that is your safety net.
2. **Wait for `addon-release.yml` green**, then verify:
   `docker pull ghcr.io/florianhorner/mammamiradio-addon-aarch64:X.Y.Z`.
3. **Open the next RC immediately** so the number keeps meaning something and CI stays green:
   - `pyproject.toml` + `ha-addon/mammamiradio/config.yaml` → `X.Y+1.0`
   - **ha-addon CHANGELOG**: add a new `## [X.Y+1.0]` header at the top. REQUIRED —
     `pre-release-check.sh` compares `config.yaml` to the first *versioned* ha-addon CHANGELOG
     header (it skips `## Unreleased`); without the new header the next version-touching PR fails.
   - **root CHANGELOG**: roll `## [Unreleased]` (plus the pending `## [X.Y.Z]`) into a single
     dated `## [X.Y.Z] - <real tag date>`, then open a fresh `## [Unreleased]`.
   - Land as a normal `chore(release): open X.Y+1.0` PR via `/ship`.

**Changelog must match the tagged commit.** If the edge SHA you tag is behind `HEAD`, fold only
the notes actually in that SHA — never publish notes for commits the promoted image lacks.

**Known limitations (revisit if they bite):**
- `release-cooldown.yml` only fails *red* on a tag <24h after the prior release; it does not
  actually block `addon-release.yml` from promoting. Don't push the tag inside the window (or use
  the `hotfix` label) rather than relying on it to stop you.
- `docker.yml` publishes the standalone image on any `v*` tag even if the addon pre-flight fails.
- A hotfix after you've opened the next RC (e.g. `2.13.1` once `main` is on `2.14.0`) needs a
  release branch, because pre-flight requires `config.yaml` and `manifest.json` == tag.

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
| `ha_context_enabled` | `bool?` | `MAMMAMIRADIO_HA_CONTEXT_ENABLED` (on by default; turn off to keep HA entity publishing but stop full `/api/states` prompt-context polling) |
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
| `jamendo_client_id` | `password?` | `JAMENDO_CLIENT_ID` (advanced optional field) |

Additional Jamendo tuning can be set in `radio.toml` or container env without exposing new Supervisor UI options: `JAMENDO_COUNTRY`, `JAMENDO_ORDER`, and `JAMENDO_LIMIT` (`1`-`200`).

**Provider secrets.** The five AI/TTS provider credentials live in `/config/secrets.env` in add-on
mode: `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `AZURE_SPEECH_KEY`, `AZURE_SPEECH_REGION`, and
`ELEVENLABS_API_KEY`. They are no longer add-on schema fields at all (unlike `JAMENDO_CLIENT_ID`,
which stays as an advanced optional Supervisor option), so a fresh install never exposes them to
`ha addons info`. Upgraded installs keep their keys through a one-time boot recovery: Supervisor
strips schema-removed keys from `/data/options.json` when it starts the add-on, so `run.sh` fetches
the values still held in Supervisor's stored settings via the Supervisor API
(`GET $SUPERVISOR_API/addons/self/info`, token-authenticated, 5s timeout, best-effort) and persists
them into `secrets.env` — every later boot is file-first. The stored copies remain visible to
`ha addons info` until the operator opens the add-on Configuration tab and saves once (which
replaces stored settings with only the current fields). `JAMENDO_CLIENT_ID` and `ADMIN_TOKEN`
remain Supervisor options. `/config/secrets.env` is plaintext in the add-on config storage, not
Home Assistant `/config/secrets.yaml`; anyone with host/add-on config access can read it.

`secrets.env` grammar is intentionally small: `KEY=VALUE` lines, optional `export KEY=VALUE`,
whitespace around keys or values, single or double quoted values, values containing `=`, UTF-8 BOM,
and CRLF endings are accepted. Full-line comments beginning with `#` are ignored. Inline comments are
not special for unquoted values, so `OPENAI_API_KEY=sk#abc` means the value contains `#abc`.

**AI quality / model selection.** `quality_profile` (premium | balanced | economy)
replaced the old `claude_model` dropdown. The operator picks *intent*, not a model
snapshot, and `run.sh` maps it to `MAMMAMIRADIO_QUALITY` (a missing/blank value
defaults to `balanced`). Creative work uses Opus/large in `premium`, Sonnet/small
in `balanced`, and Haiku/small in `economy`; latency-sensitive `fast` work stays
on Haiku/small in every profile. If an existing
`/data/options.json` still contains the removed `claude_model` key, `run.sh` also
exports it as the legacy `CLAUDE_MODEL` fast-role override until the operator saves
`quality_profile`. The canonical model IDs, OpenAI TTS selection, and
script-token prices live in the root `model_registry.toml` (see "Dynamic LLM
routing" in the root `CLAUDE.md`).
**To add or swap a model:** update the relevant registry catalog entry and its
matching `[pricing.catalog.<provider>]` key in the same change—no code or schema
change. The add-on image copies this canonical root file; do not create an
add-on-specific registry copy. An unknown experimental `--models` candidate in
the evaluator uses the registry's conservative fallback price and is marked
unpriced in its JSONL output.

The option extraction in run.sh uses a single guarded Python script that reads keys from `/data/options.json` and overlays non-empty `/config/secrets.env` values for the five provider keys. Tuple-loop option keys export as UPPER_CASE names (`jamendo_client_id` → `JAMENDO_CLIENT_ID`); behavior toggles with app-specific env vars are mapped explicitly (`enable_home_assistant` → `HA_ENABLED`, `ha_context_enabled` → `MAMMAMIRADIO_HA_CONTEXT_ENABLED`, `ha_context_poll_interval` → `MAMMAMIRADIO_HA_CONTEXT_POLL_INTERVAL`, `super_italian_mode` → `MAMMAMIRADIO_SUPER_ITALIAN`, `chaos_mode_active` → `MAMMAMIRADIO_CHAOS_MODE`, `festival_mode` → `MAMMAMIRADIO_FESTIVAL_MODE`, `broadcast_chain` → `MAMMAMIRADIO_BROADCAST_CHAIN`, `ha_media_player_push` → `MAMMAMIRADIO_HA_MEDIA_PLAYER_PUSH`, `guest_host` → `MAMMAMIRADIO_GUEST_HOST`, `quality_profile` → `MAMMAMIRADIO_QUALITY` defaulting to `balanced`). Pacing options export only when an integer value is present (`songs_between_banter` → `MAMMAMIRADIO_PACING_SONGS_BETWEEN_BANTER`, `songs_between_ads` → `MAMMAMIRADIO_PACING_SONGS_BETWEEN_ADS`, `ad_spots_per_break` → `MAMMAMIRADIO_PACING_AD_SPOTS_PER_BREAK`); malformed values are skipped so one bad key cannot drop every export. To add a new non-provider option:

1. Add to `schema:` in `config.yaml`; also add to `options:` in the same relative order only if it should be visible by default
2. Add a translation entry in `translations/en.yaml`
3. Add the run.sh export, either in the tuple loop for direct UPPER_CASE keys or as an explicit mapping for app-specific env vars
4. Read it in `config.py` via `os.getenv("MY_OPTION", "default")`

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
- `io.hass.version` — match the running image against `config.yaml`'s `version:` field. Without this label the Supervisor cannot determine whether the installed image is current.
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

`addon-release.yml` does not rebuild the add-on. It verifies that both per-arch `:${git_sha}` images exist, smoke-tests the amd64 SHA image before stable publishing, promotes those exact images to `:X.Y.Z` without changing the source manifest shape, updates `:latest` only when the current tag is the newest stable semver, and then smoke-tests the published amd64 `:X.Y.Z` image. The source `:sha` image is built with `io.hass.version` set to the stable `config.yaml` version because it may later become the stable release artifact. If a previous run published one architecture and then failed, a rerun is allowed only when the existing `:X.Y.Z` tag digest matches the source `:sha`; mismatched stable tags fail and must be cleaned up manually.

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

The pin **may trail `origin/main` HEAD**: when the tip commits touch only files outside the image paths (`ha-addon/**`, `mammamiradio/**`, `pyproject.toml`, `radio.toml`), `Build HA Addon` never ran for them and no `:<sha>` image exists, so pinning HEAD would make the Supervisor pull a missing tag. The script pins the last *built* commit instead, and **hard-fails (no PR)** rather than warn-and-continue when it cannot find a successful build run, when `gh` cannot be queried, or when an image file changed between the built commit and HEAD (which means the newest image-affecting commit has not gone green yet — wait for it, or fix the failed build). It uses `gh run list` (needs only `actions:read`); it no longer calls the GHCR packages API (which needed the `read:packages` scope the maintainer token lacks and 403'd into a soft-pass).

Because *you* open the PR (not a bot / `GITHUB_TOKEN`), its required checks (`quality`, `pi-smoke`) run normally and you merge it like any PR — no protected-branch fight, no self-merging CI, no races. Stable is never touched. (This replaced an auto-bump CI job that opened a PR and busy-waited on its own checks; it raced check-creation and orphaned PRs — see #384 / #476 / #487.)

**Constraint:** `Build HA Addon` is push-only (it does not run on PRs), so it must never be a required check on `main` — requiring it would make every PR unmergeable.

**Smoke runs in addon mode.** Every smoke `docker run` (`addon-build.yml`, and both blocks in `addon-release.yml`) sets `-e SUPERVISOR_TOKEN=smoke-ci`, mirroring how the HA Supervisor launches the image. Without it the container boots in standalone mode, where binding `0.0.0.0` with no admin token is a fatal config error (`config._is_addon` is false), uvicorn never starts, and the smoke fails with `/healthz` connection-refused — a false negative that doesn't reflect the real addon. Keep the token on any new smoke step.

**Switching the soak Pi to edge.** Edge and stable both use `host_network: true` and port 8000 — they cannot run at the same time. Uninstall stable, install "Mamma Mi Radio (Edge)" from the same Apps catalog entry, re-enter API keys. Reverse it to go back.

**Editing the edge add-on.** Its `options`/`schema` MUST stay identical to stable — edge runs the same image and the same `run.sh` reads the options. `scripts/validate-addon.sh` fails CI on any drift. When you add a config option to stable (the THREE-files contract above), the edge `config.yaml` and `translations/en.yaml` are a fourth and fifth file to update in the same commit. The edge `version:` line is the only field that changes to cut a release, and `make edge-release` does that for you.

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
  date before merging) since 2026-06-12. Dependabot PRs that fall behind get
  an automatic `@dependabot rebase` comment (`dependabot-nudge.yml`) because
  Dependabot only self-rebases on conflicts. **Live proof pending:** confirm
  on the first weekly batch that Dependabot honors the nudge comment authored
  by `github-actions[bot]`; if it ignores it, comment `@dependabot rebase`
  with your own gh auth (`gh pr comment <PR#> --body "@dependabot rebase"`)
  and demote the workflow to advisory.
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
- [ ] If new config option: added to config.yaml + run.sh + translations
- [ ] If path changed: grep all files for the old path
- [ ] If renamed anything: `grep -r "old_name" .` returns zero hits
- [ ] Landing goes through `scripts/land-pr.sh` (see "Landing a PR" above) —
      `scripts/check-merge-gate.sh` passes if anything about merging looks off

**After merging a version-bump commit** (to publish the stable image):
1. Wait for `addon-build.yml` to pass on the merged commit
2. `git tag vX.Y.Z && git push origin vX.Y.Z`
3. `addon-release.yml` runs pre-flight → smoke-prebuilt → promote → smoke; check Actions for green
4. Verify: `docker pull ghcr.io/florianhorner/mammamiradio-addon-aarch64:X.Y.Z`

## Release invariants gate (2026-04-27 onward)

`scripts/check-release-invariants.sh` runs on every PR via `quality.yml`. It catches audio delivery invariants that have caused production silence incidents, plus a release-beat manifest check:

1. **FFmpeg `music_eq_chain` eq count**: must be exactly 2. A 3rd `equalizer=` filter in `mammamiradio/audio/normalizer.py` triggers FFmpeg 8.x SIGABRT on Pi aarch64. Local: `bash scripts/check-release-invariants.sh`.
2. **Packaged recovery audio**: at least one MP3 must ship under `mammamiradio/assets/demo/recovery/`, and `producer.py` must not call `generate_silence` in recovery paths.
3. **`_pick_canned_clip=None` test mock**: at least one test file must mock this to `None`. Tests that return a real file hide the empty-container / missing-packaged-clip scenario that can happen in a broken image.
4. **`session_stopped` test**: at least one test file must reference `session_stopped`. Covers the post-restart scenario where the HA watchdog restarts the addon with the flag still set.
5. **HA Green fallback performance gates**: `QUEUE_FALLBACK_WAIT_SECONDS` stays <= 5s, the norm-cache rescue avoids deterministic first-file selection, and the HA Green perf/launch smoke scripts + Make targets exist.
6. **Release beat source manifest**: `scripts/validate-release-beat.py` (no args) checks that `mammamiradio/assets/release/release_beat.toml`, if present and enabled, has valid schema, listener-safe copy, and is declared in `pyproject.toml` package-data. A missing or explicitly disabled manifest passes as a no-op.

**Version sync check**: also wired into every PR. If `pyproject.toml` or `ha-addon/mammamiradio/config.yaml` appears in the PR diff, CI runs the full `scripts/pre-release-check.sh` (version consistency + CHANGELOG head + all invariants). No-ops on non-version PRs. This closes the version-drift class of bug that caused the stale 2.10.7→2.10.9 CHANGELOG incident.

Local pre-release: `make pre-release` (runs full `pre-release-check.sh`, all 7 checks — including a target-scoped release-beat check: `--channel stable --semver "$ADDON_VER"`, which additionally confirms the manifest's channel/semver match the release being cut when the manifest is enabled).

## Release cooldown (stabilization run, 2026-04-17 onward)

A 24-hour minimum gap is enforced between consecutive published releases. The gate is `.github/workflows/release-cooldown.yml`; it runs on every `v*` tag push and queries GitHub Releases for the prior published (non-draft, non-prerelease) release's `publishedAt`.

- Block rule: `prior_release_time + 24h > now` => status check fails, release surfaces red.
- Bypass: the PR that introduced the tagged commit carries the `hotfix` label. The workflow skips the cooldown check entirely. Intended for P0/P1 regressions the existing release just introduced.
- Override: `MIN_COOLDOWN_HOURS=<n>` at workflow level (not set by default) tightens or relaxes the window.
- Self-test: `bash tests/workflows/test_cooldown_gate.sh` runs 9 scenarios (1h / 24h boundary / 25h / MIN_COOLDOWN_HOURS override / malformed ISO / clock skew / no-prior). Wired into `quality.yml` — runs on every PR.

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

## Expected log signatures after a release

Use these to tell intentional degradation from a real regression during post-merge verification and soak runs.

**Healthy startup**: boot summary line, one `Producing MUSIC:` within a few seconds, no repeated `queue empty` warnings.

**Anthropic auth suspended (intentional)**: one `Anthropic auth failed — suspending for 10 minutes` followed by OpenAI script generation. If you see this line repeating every few seconds, the WS3-A cooldown broke.

**TTS voice substituted (intentional)**: one `Invalid voice 'X' for backend edge; falling back to it-IT-DiegoNeural` at boot. Zero per-segment `Invalid voice` lines. Dashboard shows `tts_degraded` badge.

**Chart content filter (intentional)**: `INFO Rejecting non-music chart entry: …` and `INFO Chart ingest: filtered N non-music entries` each time the chart is refreshed. Normal values are 0-3 rejections per refresh.

**Session track denylist (intentional)**: `WARNING Skipping track due to invalid download (…): …` plus `WARNING Purged rejected cache file …` when a download fails validation. Subsequent reselections log `DEBUG Skipping denylisted track (already rejected this session)` instead of retrying.

**Queue starvation rescue (intentional)**: `Queue empty Ns - rescuing with canned clip` or `… with norm cache` or `… with demo asset` within 30-60s of silence. A forced-banter `force_next = BANTER` after 60s is the last-resort escape.

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

### Version shows but update fails
- GHCR image might not exist for the version in config.yaml
- Check: `docker pull ghcr.io/florianhorner/mammamiradio-addon-aarch64:VERSION`
- If not found: CI didn't run or failed, check Actions tab

## Hardcoded values that must stay in sync

| Value | Files |
|-------|-------|
| Port 8000 | config.yaml (`ingress_port`), run.sh (`MAMMAMIRADIO_PORT`, `--port`), config.py (default) |
| `MAMMAMIRADIO_ALLOW_YTDLP=true` | run.sh (hardcoded, required for chart music playback) |
| `MAMMAMIRADIO_LEDGER_ENABLED=true` | run.sh (hardcoded, enables per-segment provenance ledger in the addon; data stays local at `/data/cache/ledger/`) |

If you change any of these, grep for the old value and update all locations.
