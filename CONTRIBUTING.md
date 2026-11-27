# Contributing

The station has real moving parts: FastAPI, FFmpeg, Edge TTS, an optional AI writer (Anthropic or OpenAI) and optional Home Assistant. The fastest way to break it is to change behavior without running it, so: set up, run targeted tests, then listen.

## TL;DR

```bash
python3.11 -m venv .venv && source .venv/bin/activate
python -m pip install -r requirements-dev.txt
python -m pip install --force-reinstall --require-hashes -r requirements.txt
python -m pip install --no-deps -e .
python -m pip check
cp .env.example .env
./start.sh                # or: docker compose up
pytest tests/core/test_config.py -q  # fast loop while editing
make check                         # complete gate before committing
```

## Prerequisites

- Python 3.11+
- FFmpeg on your `PATH`
- Optional: Anthropic and/or OpenAI credentials for the full AI radio experience

No AI or music-provider key is required: host writing falls back to stock copy
and fallback voices, and the attributed starter collection provides offline
music. Optional sources (default-off Jamendo, operator-supplied MP3s in
`music/` that the project has not cleared, the standalone `external-media`
extra) and their rights limits are in [Music sources and rights
boundaries](docs/music-sources.md).

## Local setup

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-dev.txt
python -m pip install --force-reinstall --require-hashes -r requirements.txt
python -m pip install --no-deps -e .
python -m pip check
cp .env.example .env
```

Install developer tools first, then the hash-locked runtime dependencies. Installing the application with `--no-deps` preserves those versions; `pip check` verifies its declared requirements. Quality CI and both container builds use the same runtime lock. The dependency tests also verify the requested runtime extras against installed package metadata.

uv does not manage this environment: `pyproject.toml` sets `[tool.uv] managed = false`, so `uv sync`, `uv lock` and `uv add` refuse, and `uv run` uses the existing `.venv` without syncing it (unless another environment is active, `.venv` is missing, or `--project` points here from outside). `uv pip install`, `uv pip sync` and `uv venv --clear` still write to `.venv`; use the pip commands above instead. If the dependency tests report a drifted package, repeat the installation above.

Then fill in whatever `.env` values you need:

- `ANTHROPIC_API_KEY` for banter and ad script generation (falls back to OpenAI if unavailable)
- `OPENAI_API_KEY` as a script generation fallback
- `ELEVENLABS_API_KEY` for the premium cast; without it every voice uses its free Edge double
- `HA_TOKEN` for Home Assistant prompt context
- `ADMIN_PASSWORD` or `ADMIN_TOKEN` if you plan to bind outside localhost

`radio.toml` is the main station config. That is where you change hosts, pacing, ad brands, audio settings, and Home Assistant enablement.

### Updating the dependency lock

For a targeted runtime update, activate a Python 3.11 environment and install the pinned lock-generation tool first. Replace `PACKAGE==VERSION` with the dependency and version you intend to update:

```bash
python -m pip install pip-tools==7.6.1
python -m piptools compile --generate-hashes --output-file=requirements.txt --strip-extras --upgrade-package PACKAGE==VERSION pyproject.toml
```

Review all changed pins and hashes, then repeat the installation above and run `make check`. Keep Pydantic and its exact Pydantic Core dependency together. Build-system dependencies remain separately resolved by pip's isolated build environment.

#### Completing a Dependabot lock

Dependabot rewrites only the pins it bumps in `requirements.txt` and never re-resolves the lock. CI installs with `--require-hashes` (`.github/actions/setup-python-ci/action.yml`), so a bump that adds or moves a transitive dependency fails at install time, not in a test.

To complete such a PR, check out the Dependabot branch and merge `origin/main` if the branch is behind (merge, never rebase: the first commit must stay Dependabot's so the auto-merge metadata remains valid). Run the `piptools compile` command above with one `--upgrade-package PACKAGE==VERSION` per package the PR bumps; for the pydantic group pass `pydantic` and `pydantic-settings` only, because pip-compile selects the matching `pydantic-core`. Repeat the installation above, run `make check`, commit as `chore(deps): complete the lock for <package> <version>`, and push to the Dependabot branch. The pushed commit re-runs CI and keeps patch and minor PRs eligible for the guarded auto-merge. Majors land through a migration PR instead.

When `ruff==` moves in `requirements-dev.txt`, set `rev:` under the `ruff-pre-commit` repo in `.pre-commit-config.yaml` to `v<version>` in the same commit (`tests/repo/test_repo_scripts.py` enforces the pair). Never bump `.pre-commit-config.yaml` alone.

## Run the app

Full dev workflow:

```bash
./start.sh
```

Or use Docker (no Python/FFmpeg setup needed):

```bash
docker compose up
```

If you only need the web app and background tasks:

```bash
source .venv/bin/activate
python -m uvicorn mammamiradio.main:app --reload --reload-dir mammamiradio
```

Useful URLs:

- `http://127.0.0.1:8000/` — listener page for public callers; flips to the admin control room when the request carries a trusted HA ingress header
- `http://127.0.0.1:8000/listen` — explicit listener alias (always the public UI)
- `http://127.0.0.1:8000/admin` — admin control room (open on loopback; other callers follow the [Admin access model](docs/operations.md#admin-access-model))
- `http://127.0.0.1:8000/stream` — infinite MP3 stream
- `http://127.0.0.1:8000/public-status` — public JSON
- `http://127.0.0.1:8000/status` — admin JSON

Conductor users: workspace setup, run and archive hooks are described in [docs/conductor.md](docs/conductor.md).

### Stream-alive reload (optional)

By default, `./start.sh` runs uvicorn with `--reload`. Every file save restarts the
process and drops active stream connections.

Install [caddy](https://caddyserver.com/docs/install) to keep streams alive across
reloads — caddy holds the client connection and reconnects to uvicorn transparently:

```bash
brew install caddy   # macOS
# or: apt install caddy   # Ubuntu/Debian
```

With caddy installed, `./start.sh` automatically uses it:
- caddy listens on `$PORT` (the address you connect to, default `:8000`)
- uvicorn listens on `$PORT+1` (internal only, proxied by caddy)
- Active `/stream` connections survive `--reload` restarts of up to ~30 seconds

Without caddy, `./start.sh` falls back to bare uvicorn with a warning.

### Tune host writing without a stream gap

Host behavior lives in `mammamiradio/hosts/scriptwriter.py` (generation logic)
and its prompt-data modules in the same folder:

- `prompt_world.py`: expression banks, host fingerprints, exchange shapes and
  lore, Chaos and Festival mode blocks
- `relationship.py`: roster-aware shape and lore selection, recency
- `transitions.py`: transition rewrite openers
- `fallbacks.py`: chaos stock lines, ad-break bumpers
- `station_name_guard.py`: scrubs foreign and competitor station names

After an edit, run `make check`, then `POST /api/hot-reload` (admin auth, empty
body). It reloads `language_policy`, the modules above, then `scriptwriter`,
leaves first. `POST /api/trigger {"type": "banter"}` then generates a segment
with the new code. The stream stays live throughout; if the reload fails (for
example a syntax error), the endpoint returns 500 and the old code keeps
running.

`memory_extractor.py` (post-air memory extraction) is excluded from hot-reload:
it holds in-flight extraction state that a reload would reset mid-run. Restart
to pick up changes to it.

## Fast loop and complete gate

During an edit, run the smallest relevant test file or test target. For a broader fast pass without coverage instrumentation, use `make test-fast`:

```bash
pytest tests/core/test_config.py tests/scheduling/test_scheduler.py
make test-fast
```

Before pushing, run the deterministic lint bundle (the pre-push hook runs it
automatically when installed):

```bash
scripts/pre-lint.sh
```

It mirrors the Quality lint job without auto-fixing files or starting the full
pytest/coverage run. Before opening a PR with a broad runtime or coverage
change, run `make check` for full-suite proof; required CI still decides.

Notes:

- The suite never reads your `.env`: `core/config.py` loads it at import time, so `tests/conftest.py` sets `PYTHON_DOTENV_DISABLED=1` before collection. Without that, a local `.env` with `HA_URL` or `HA_ENABLED` fails tests that pass in CI. It needs `python-dotenv>=1.2` (`tests/core/test_config_env_overrides.py` checks). The `_isolate_env` fixture also strips exported provider credentials, HA settings and port overrides.
- `tests/hosts/test_ads.py` and `tests/audio/test_normalizer_real_ffmpeg.py` need FFmpeg; the real-FFmpeg tests skip without it. The [Pi aarch64 smoke](#pi-aarch64-smoke) job runs them on ARM to catch aarch64-specific crashes.
- Home Assistant add-on changes must also pass the local add-on build check:

```bash
scripts/validate-addon.sh
```

That command checks the same add-on invariants CI validates. Add `--build` when you also want the slower local container build. If it fails locally, do not commit or push.

For Home Context Director changes, first run the credential-free contract path:

```bash
pytest tests/home/test_context_director.py tests/home/test_entity_policy.py tests/core/test_home_context_director_lifecycle.py tests/web/test_context_director_public_metadata.py \
  tests/web/test_streamer_routes.py -k "personal_moment or home_fact or entity_policy"
```

It covers safe selection, policy migration, queue settlement, public metadata exclusion, and the `PATCH /api/homeassistant/entity-policy` contract (consent gate, queue purge, in-flight reservation release) without a live Home Assistant or provider key.

### Coverage floors and the ratchet

Coverage must stay above the committed minimums. Two layers enforce this:

- **Aggregate floor**: `fail_under` in `pyproject.toml` — the overall minimum.
- **Per-module floors**: `.coverage-floors.json` — every module has its own floor. A module-level regression fails CI even if the aggregate stays above threshold.

CI enforcement: `.github/workflows/quality.yml` runs `scripts/coverage-ratchet.py`:

- On PRs: `check` mode — fails if any module dropped below its floor.
- On `main`: `update` mode recomputes the floors and pushes them. If the push is
  rejected (protected branch), it opens a `coverage-ratchet-stale` issue with the
  diff: run `make coverage-ratchet` locally, verify the increases, and open a PR
  with `Fixes #<issue number>`. The issue stays open until then, because a later
  run cannot prove the earlier increase landed. A failed computation opens a
  separate `coverage-ratchet-broken` issue that closes when computation recovers.
- The `tests` job checks out full git history and every tag (`fetch-depth: 0`).
  The imaging pack provenance test reads generator inputs at the revision pinned
  in the manifest, which a shallow clone cannot prove and which may be reachable
  only through a `provenance/audio-pack-*` tag. If the validator reports
  `generator revision … is not in this clone`, run the `git fetch origin tag …`
  command it prints and rerun the test.

Local check: `make coverage-check` to verify locally. `make coverage-ratchet` to preview what CI would commit.

Adding tests: CI computes higher floors after merge. They become enforced only
after the updated floor files land; follow any ratchet issue to finish that step.

### Pi aarch64 smoke

`.github/workflows/pi-smoke.yml` is an arm64 FFmpeg
7.x/8.x compatibility canary, not exact add-on package parity. It uses JVS as
primary and verifies the supported BtbN fallback against GitHub's digest.
The required `pi-smoke` check always reports; the ARM job itself runs on
every `main` push and on PRs that touch audio, scheduling, streamer,
startup/core configuration, launch-smoke, Python package/runtime/development
dependencies, radio/model configuration, the changed-lanes classifier, or
the shared Python CI setup.

## Bundled starter media

`mammamiradio/assets/starter/catalog.json` is the only starter-catalog
authority. Do not add a parallel title list, hand-edit hashes, copy audio into
the package directly, or use the workflow for an arbitrary URL. The tooling
reports source and audio facts; a human still owns the full audition and rights
review.

Acquire only one exact predeclared catalog row:

```bash
python scripts/starter-catalog.py acquire --isrc USUAN1900042
```

Two providers are supported, selected by the row's `provider` field. Absent
means Incompetech, which needs no credential. A `provider: jamendo` row needs a
free client id, because Jamendo's public track page publishes no licence at all
and the API is the only authority:

```bash
JAMENDO_CLIENT_ID=... python scripts/starter-catalog.py acquire --isrc JAMENDO-1093607
```

Only `acquire` needs the key — the licence response is hashed into the receipt,
so `check` stays entirely offline.

Bundled music is **attribution-only**, and this is not a preference. A track
carrying NonCommercial, NoDerivatives or ShareAlike cannot ship no matter how
good it sounds. NoDerivatives is the one that catches people out: every bundled
track is normalized to a fixed loudness and re-encoded, which is a derivative,
and ND forbids distributing one.

The command prints a candidate ID and writes source evidence under ignored
`tmp/starter-media/`. Stage that exact candidate into the deterministic station
format:

```bash
python scripts/starter-catalog.py stage --candidate <candidate-id>
```

Listen to `staged.mp3` from start to finish. Create a decision file outside the
repository (for example `/tmp/starter-decision.json`) with this exact shape:

```json
{
  "schema_version": "1",
  "isrc": "USUAN1900042",
  "reviewed_at": "2026-07-16T12:00:00Z",
  "reviewer_role": "release maintainer",
  "listened_from_start_to_finish": true,
  "title_and_artist_match": true,
  "no_unexpected_speech_or_restricted_content": true,
  "editorially_approved": true,
  "license_evidence_reviewed": true
}
```

Do not set a field to `true` without performing that review. Approval installs
the derivative, redacted receipt, and manifest row as one bounded operation:

```bash
python scripts/starter-catalog.py approve \
  --candidate <candidate-id> \
  --replace USUAN1900042 \
  --decisions /tmp/starter-decision.json
```

Then inspect the manifest/audio/receipt diff and run:

```bash
make media-check
make media-proof
```

`media-check` is the fast offline manifest, evidence, byte, and audio check;
until the starter content lands it reports missing content without failing,
while `scripts/pre-release-check.sh` enforces the same proof hard on the
release path.
`media-proof` additionally proves wheel, sdist, amd64/aarch64 add-on image,
FFprobe, extractor-containment, and Jamendo-transience parity. A release remains
blocked until all twelve exact derivatives total at least 45 minutes, remain at
or below 75 MiB, and have complete evidence and audition receipts. Twenty cold Home Assistant Green listener runs at p95 first accepted non-silent
starter byte within two seconds are an opt-in gate, armed with
`MMR_REQUIRE_HA_RECEIPTS=1`; unset, a release cut reports the waiver.

To replace a mistaken approval before publication, revert the derivative,
receipt, and the same manifest row together; never leave one of the three at a
new revision. Discarding an unapproved ignored candidate changes no packaged
state. The starter shuffle bag derives its cycle identity from the manifest
digest, plays all twelve before repeating, and starts a fresh complete cycle
when a new catalog is loaded; do not add or edit separate cycle-reset state.

A bundled-media change also updates the Unreleased sections in both changelogs.
Version files change only in an explicitly authorized release.

### Auditioning a new ad voice

New ad identities are staged by default: `airtime_approved` stays `false`
until a provider render and a human review approve them; only then set it
explicitly to `true`. Render the configured samples with real credentials
(audio and manifests stay ignored under `tmp/voice-auditions/`):

```bash
./.venv/bin/python scripts/audition_tts_voices.py --config radio.toml --providers elevenlabs --strict
```

When upgrading an existing custom roster, review every `[[ads.voices]]` row:
omitting `airtime_approved` now means `false`. Set it to `true` only for a
character with existing provider and human approval. A legacy role pin can keep
serving its campaign while a replacement waits for approval. Otherwise an
unapproved direct character's campaign is excluded and another safe campaign is
tried; with none left, the station tries an approved packaged ad before
continuity. It never recasts the character.

For an on-demand A/B comparison of ElevenLabs stability, generate all variants
from the same configured text and compare the profile-labelled clips in the
same ignored audition directory:

```bash
./.venv/bin/python scripts/audition_tts_voices.py \
  --config radio.toml --providers elevenlabs --strict \
  --elevenlabs-stability 0.42 0.60 0.75
```

Alternate matching clips at the same volume when listening. Differences can be
subtle and voice-specific; this command creates comparison samples only and
does not record or apply an approval.

Listen before changing `airtime_approved`. Record the human decision with the
receipt mode. `--selection-manifest` and `--selection-decisions` must be
supplied together: the ignored manifest is the completed provider run, and the
local JSON array contains only the profile-aware `candidate_id` copied from
that manifest, `candidate_name`,
`approval_status`, and a controlled `rationale` code. Accepted codes are
`accepted_clear_natural_delivery`,
`accepted_distinct_character`, and `accepted_balanced_brand_fit`; rejected
codes are `rejected_provider_failure`, `rejected_unintelligible_delivery`,
`rejected_unconvincing_character`, `rejected_off_brand_delivery`, and
`rejected_profile_mismatch`:

```bash
./.venv/bin/python scripts/audition_tts_voices.py \
  --selection-manifest tmp/voice-auditions/audition-YYYYMMDDTHHMMSSZ/manifest.json \
  --selection-decisions /tmp/ad-voice-decisions.json \
  --selection-receipt-path proof/YYYY-MM-DD-voice-selection.json
```

Use a distinct tracked receipt path for each reviewed selection. Without
`--selection-receipt-path`, the command uses
`proof/2026-07-13-voice-diversity-selection.json` and refuses to replace an
existing receipt. `--overwrite-selection-receipt` deliberately replaces that
chosen receipt; use it only to correct the same review, never to start a new
audit history.

The shipped cast's receipt is `proof/2026-10-22-speech-cast.json`. The test
suite runs `scripts/check_speech_cast.py --binding`, which fails when an
airtime ad voice or the station ident has no accepted row there, so a recast
lands together with its new rows. An Edge audition that fails is recorded as
failed, never as the house voice the station would have aired instead.

Accepted media and human-review receipts are immutable. Historical review
receipts under `proof/preship-reviews/v2/` remain readable with the standalone
verifier; they are no longer required for a PR or landing, so do not refresh
them or recreate retired fixed-name proof files. Review, required checks, and
normal landing gates still apply.

The redacted tracked proof stores the candidate ID and name, selected profile,
text and audio hashes, provider result, duration, approval status, and human
rationale code only. It never stores raw audition copy, audio, local paths, or
credentials.

## Lint, format, and type check

```bash
ruff check .          # lint
ruff check --fix .    # lint + auto-fix
ruff format .         # format
ruff format --check . # format check (CI mode)
mypy mammamiradio/ tests/  # type check
```

`scripts/ruff.toml` extends the root Ruff policy and ignores only `N999`: the
hyphenated script filenames are shell entry points, not Python modules.

To install pre-commit hooks locally:

```bash
pip install pre-commit
pre-commit install --hook-type pre-commit --hook-type pre-push
```

The repo runs `scripts/pre-lint.sh` on pre-push and wires
`scripts/validate-addon.sh` into both `pre-commit` and `pre-push` for files that
can break the Home Assistant add-on build. The lint bundle requires ShellCheck
0.11.0 or Docker; Docker Desktop or Podman is also required for add-on `--build`
checks.

### UI copy lint

`scripts/check-ui-copy-lint.sh` is the [leadership principle #5](CLAUDE.md#leadership-principles) guard over human-facing
strings — listener `ui_copy`, the listener/clip/admin templates, `listener.js`/`admin.js`,
HA add-on option descriptions, and the streamer setup errors. Three rules: machine words on
a human screen (`tech_lingo`) and copy still phrased around picking a speaker or room
(`stale_speaker_copy`) always fail the lint. A stated failure with no next step
(`no_way_out`) fails the lint only for copy authored as a structured table row
(`WAY_OUT_BLOCKING_CONTEXTS`, matched on the context group, not as a string prefix) —
copy reviewed as a set rather than written inline at a call site. Two of those groups
give each failure its own `action` field; the other two are single authored sentences
carrying their own remedy. That every row passes is recomputed by
`test_every_blocking_context_row_carries_a_way_out`, not restated as a count.
In free-text toasts it never fails the lint, because a fixed verb list cannot enumerate
English imperatives and would flag correct copy — it is reported by `--audit`, which
labels every row blocking or advisory, and counted against `MAX_ADVISORY_VIOLATIONS`.
That ceiling does fail CI: free text is bounded, not unwatched. Raising it is the
deliberate edit that says the check misjudged a string, which keeps good copy out of
the baseline where it would look like a real violation.
Admin/addon/server copy is held to the same banned-word list as the listener — a warmer
register, not a shorter list.

- Known violations are grandfathered by fingerprint in `.config/ui-copy-baseline.json`;
  CI fails on NEW ones. Refresh with `bash scripts/check-ui-copy-lint.sh --write-baseline`,
  which prints every violation it newly accepts so a refresh cannot silently absorb a
  regression. A baselined violation that no longer reproduces is also a failure — the
  baseline may only shrink. `MAX_BASELINED_VIOLATIONS` and `MAX_ADVISORY_VIOLATIONS` in
  `tests/repo/test_ui_copy_lint.py` pin both ceilings, so advisory findings cannot pile up
  unwatched just because they do not fail the build.
- `MIN_STRINGS_PER_GROUP` is a coverage floor per context group. The lint scrapes copy out
  of templates, so an extractor that stops matching after a reformat would otherwise
  collect nothing, find no violations, and report "clean" — the floors make that a failure
  instead. A group is one surface, enforced by
  `test_each_surface_file_has_its_own_coverage_floor`: two files sharing a group would
  share a floor, and the larger file's count alone would keep the smaller one's collapse
  invisible. The two add-on translation files are the one allowed exception, since they
  are copies of each other.
- Runs in `quality.yml` (`lint` job) beside the changelog and docs-safety lints.
  Local: `bash scripts/check-ui-copy-lint.sh` (add `--audit` for the full report; every
  flag is forwarded to `scripts/ui_copy_lint.py`).

## Manual smoke test

After starting the app:

1. Open `http://127.0.0.1:8000/` and confirm the listener page loads.
2. Open `http://127.0.0.1:8000/admin` (with admin auth if non-loopback) and confirm the control room loads.
3. Open `http://127.0.0.1:8000/stream` in a browser or player and confirm audio starts once the first segment is queued.
4. Hit `/public-status` and confirm the upcoming list reflects only real queued segments, or returns `upcoming_mode="building"` while no render-ready segment exists yet.
5. Use the control room for skip, shuffle, purge, and playlist reorder.
6. Restart the app and verify the last selected source restores automatically.

If you are binding to `0.0.0.0`, set `ADMIN_PASSWORD` or `ADMIN_TOKEN` first or config validation will reject startup. Non-loopback admin requests with basic auth also require CSRF validation (the dashboard handles this automatically via injected tokens).

For First Listen or any Home Assistant-facing manual QA, use the
[disposable local Home Assistant lab](docs/runbooks/first-listen-local-ha.md).
It exercises a real HA Media Source and Mac VLC `media_player` without exposing
branch code, test credentials, or synthetic state to a real home.

## Documentation expectations

When behavior changes, update the matching docs in the same change:

- `README.md` for user-facing setup and route changes
- `ha-addon/README.md` and `ha-addon/mammamiradio/DOCS.md` for Home Assistant
  install steps and First Listen labels
- `docs/integrations/` for integration setup and playback routes
- `docs/architecture.md` for runtime flow and system design changes
- `CLAUDE.md` for the rules coding agents follow (the codebase map is `docs/REPO_MAP.md`)
- `docs/troubleshooting.md` for failure modes users will actually hit
- `docs/operations.md` for runtime and deployment assumptions
- `CHANGELOG.md` for shipped behavior worth calling out

If you add a new config key, env var, route, auth rule, or fallback path and do not document it, the docs are wrong. Fix them in the same change.

When changing anything a listener or operator reads (template copy, `ui_copy.py`,
toast text, `streamer.py` setup errors, add-on option descriptions), run
`bash scripts/check-ui-copy-lint.sh`; [UI copy lint](#ui-copy-lint) has the rules.
Fixing a grandfathered violation also fails it until you run `--write-baseline`;
read the `+` lines that refresh prints, because each is a violation it newly
accepted.

When changing the public install or add-on guides, run `bash scripts/check-docs-safety.sh`. It catches retired Home Assistant navigation, unsafe live-recovery instructions, and broken relative Markdown links before CI does.

Public examples use synthetic entity IDs such as `person.example_resident`,
`device_tracker.example_phone`, and `lock.example_front_door`. Keep observed
household identifiers and private capture notes out of new docs and fixtures.
The documentation check enforces that convention for its selected public
examples and rejects obsolete review-receipt requirements in maintained release
guides. Legacy runtime fixtures that still use older IDs change only in a
dedicated compatibility PR.

Before publishing a PR or a curated GitHub release, save the exact body in a
local file and run `bash scripts/check-pr-body-lint.sh <body-file>`. The same
public-text rules apply to both. Internal workspace links are rejected without
echoing their identifiers into the check's output; public tool documentation and
contributor attribution remain valid. PR CI checks published text using the base
branch's rules. It cannot prevent initial publication or guarantee what another
application may append, so inspect the final payload as well.

<!-- BEGIN: commit-message-standards (managed by bootstrap-repo.sh — do not hand-edit) -->
## Commit messages

Use `type(scope): subject`, for example `fix(auth): reject expired sessions`.
Allowed types are `feat fix docs style refactor test chore ci build perf revert`.
Keep the subject at most 72 characters and omit a trailing period. The title of
a pull request opened by a dependency bot (Dependabot, Renovate, pre-commit.ci)
may be longer.

For `feat` commits with >50 lines changed, add a body line beginning with
`Why:` that explains the reason for the change. This is a convention; CI does
not fail without it.

The trusted CI workflow uses `.config/commit-lint/validate-trusted.py` from
the default branch and reads PR commit history as data. It runs the adjacent
`.config/commit-lint/validate-pr.sh` and `commit-msg`; it never executes PR files.
It checks the PR title and non-merge commits. Changes to these
policy files take effect after they merge. To check a message before pushing, run
`bash .config/commit-lint/commit-msg <message-file>`. These policy files are generated;
do not edit them by hand.

During installation, the existing check remains active. The trusted
workflow first runs as `commit-lint / trusted policy preview`. Retire
the old check only after the preview passes on the PR head and the
repository's Actions event policy permits `pull_request_target`.

<!-- END: commit-message-standards -->

## Contributors

Thanks to the people who've shaped Mamma Mi Radio:

- [@florianhorner](https://github.com/florianhorner): maintainer
- [@ashika-rai-n](https://github.com/ashika-rai-n): dashboard CSS/JS extraction into `/static/` ([PR #203](https://github.com/florianhorner/mammamiradio/pull/203))

First-time contributors are especially welcome and are protected by the merge-first protocol: an approved first-time PR lands before any maintainer refactor touches the same files, so the "Merged" badge stays earned.
