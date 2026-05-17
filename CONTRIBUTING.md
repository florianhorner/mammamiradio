# Contributing

This repo is small, but it has real moving parts: FastAPI, FFmpeg, Edge TTS, Claude, and optional Home Assistant. The fastest way to break it is to change behavior without actually running the station.

Do the local setup, run targeted tests, then do a quick listen-through.

## Prerequisites

- Python 3.11+
- FFmpeg on your `PATH`
- Optional: Anthropic and/or OpenAI credentials for the full AI radio experience

Music source fallback chain: when `MAMMAMIRADIO_ALLOW_YTDLP=true` the app blends live Italian charts with anything in `music/`; with yt-dlp disabled it plays local `music/` only; if neither is available it falls through to silence. No external service credentials are required to run the station.

## Local setup

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e .
cp .env.example .env
```

If you use Conductor, see [docs/conductor.md](docs/conductor.md) for workspace lifecycle details.

Then fill in whatever `.env` values you need:

- `ANTHROPIC_API_KEY` for banter and ad script generation (falls back to OpenAI if unavailable)
- `OPENAI_API_KEY` for TTS voices and as a script generation fallback
- `HA_TOKEN` for Home Assistant prompt context
- `ADMIN_PASSWORD` or `ADMIN_TOKEN` if you plan to bind outside localhost

`radio.toml` is the main station config. That is where you change hosts, pacing, ad brands, audio settings, and Home Assistant enablement.

## Run the app

Full dev workflow:

```bash
./start.sh
```

That script runs uvicorn with `--reload`.

Or use Docker (no Python/FFmpeg setup needed):

```bash
docker compose up
```

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

**Caddy is optional.** Without it, `./start.sh` falls back to bare uvicorn with a
warning. The audio stream will still work — it just drops on every file save.

If you only need the web app and background tasks:

```bash
source .venv/bin/activate
python -m uvicorn mammamiradio.main:app --reload --reload-dir mammamiradio
```

Useful URLs:

- `http://127.0.0.1:8000/` — listener page for public callers; flips to the admin control room when the request carries a trusted HA ingress header
- `http://127.0.0.1:8000/listen` — explicit listener alias (always the public UI)
- `http://127.0.0.1:8000/admin` — admin control room (guarded by `require_admin_access`: loopback, private network including HA Supervisor ingress, admin token, or basic auth)
- `http://127.0.0.1:8000/stream` — infinite MP3 stream
- `http://127.0.0.1:8000/public-status` — public JSON
- `http://127.0.0.1:8000/status` — admin JSON

## Tests

Fast tests:

```bash
pytest tests/test_config.py tests/test_scheduler.py
```

Full suite:

```bash
pytest tests/
```

Notes:

- `tests/test_ads.py` and `tests/test_normalizer_real_ffmpeg.py` exercise audio helpers and need FFmpeg installed. The real-ffmpeg tests skip automatically when FFmpeg is absent; the pi-smoke CI job (`ubuntu-24.04-arm`) runs them on ARM hardware to catch aarch64-specific crashes.
- Home Assistant add-on changes must also pass the local add-on build check:

```bash
scripts/validate-addon.sh
```

That command checks the same add-on invariants CI validates. Add `--build` when you also want the slower local container build. If it fails locally, do not commit or push.

## Lint, format, and type check

```bash
ruff check .          # lint
ruff check --fix .    # lint + auto-fix
ruff format .         # format
ruff format --check . # format check (CI mode)
mypy mammamiradio/ tests/  # type check
```

To install pre-commit hooks locally:

```bash
pip install pre-commit
pre-commit install --hook-type pre-commit --hook-type pre-push
```

The repo wires `scripts/validate-addon.sh` into both `pre-commit` and `pre-push` for files that can break the Home Assistant add-on build. Docker Desktop or Podman must be installed for `--build` checks.

## Manual smoke test

After starting the app:

1. Open `http://127.0.0.1:8000/` and confirm the listener page loads.
2. Open `http://127.0.0.1:8000/admin` (with admin auth if non-loopback) and confirm the control room loads.
3. Open `http://127.0.0.1:8000/stream` in a browser or player and confirm audio starts once the first segment is queued.
4. Hit `/public-status` and confirm the upcoming list reflects the real queued segments, or returns `upcoming_mode="building"` while the producer is warming up.
5. Use the dashboard controls for skip, shuffle, purge, and playlist reorder.
6. Restart the app and verify the last selected source restores automatically.

If you are binding to `0.0.0.0`, set `ADMIN_PASSWORD` or `ADMIN_TOKEN` first or config validation will reject startup. Non-loopback admin requests with basic auth also require CSRF validation (the dashboard handles this automatically via injected tokens).

## Documentation expectations

When behavior changes, update the matching docs in the same change:

- `README.md` for user-facing setup and route changes
- `docs/architecture.md` for runtime flow and system design changes
- `CLAUDE.md` for the codebase map used by coding agents
- `docs/troubleshooting.md` for failure modes users will actually hit
- `docs/operations.md` for runtime and deployment assumptions
- `CHANGELOG.md` for shipped behavior worth calling out

If you add a new config key, env var, route, auth rule, or fallback path and do not document it, the docs are wrong. Fix them in the same change.



<!-- BEGIN: commit-message-standards (managed by bootstrap-repo.sh — do not hand-edit) -->
## Commit messages

This repo follows the [engineering-standards commit-message spec](https://github.com/florianhorner/engineering-standards/blob/main/specs/commit-message-spec.md). The cheat sheet below is self-sufficient — you do not need to leave the repo to write a conformant commit.

### 30-second cheat sheet

1. **Format:** `type(scope): subject` — e.g. `fix(auth): handle expired session cookie`
2. **Allowed types:** `feat fix docs style refactor test chore ci build perf revert`
3. **Subject:** ≤72 chars total, imperative mood ("fix bug" not "fixed bug"), no trailing period, no `v1.2.3` prefix
4. **Body required only when:** type is `feat` AND >50 lines changed. Body must include a `Why: <one-line>` (rule_id `WHY_REQUIRED`)
5. **Bypass:** `--no-verify` is allowed only with a `Policy-Override: <reason>` trailer (otherwise CI blocks)

### Good examples

```
fix(auth): handle expired session cookie returning undefined
```

```
docs(readme): clarify install prerequisites
```

```
feat(curve-card): add brightness scrubber with bar gauges

Why: ops team needs at-a-glance brightness state without opening editor.
Tested: e2e curve-editor + unit tests for scrubber state.
Refs: closes #67
```

### Bad examples (with the rule_id they violate)

```
Add files via upload                                 # rule_id: WEB_UI_DEFAULT
v2.10.11 feat(jamendo): country + order filters     # rule_id: VERSION_IN_SUBJECT
chore: addressed all the review comments             # rule_id: AGENT_SELF_TALK
```

```
feat(auth): add OAuth flow

florian asked me to add this                         # rule_id: OPERATOR_ATTRIBUTION (body)
```

### Body-when-required rule

A `Why:` body line is REQUIRED when **both** conditions hold:
- type is `feat`
- `git diff --shortstat` shows >50 lines changed

For all other commits the body is optional. Acceptable terse `Why:` templates:
- `Why: closes #N` (when issue body has the context)
- `Why: incident response — outage 2026-05-08T03:00Z`
- `Why: spec at <url>; see decision log section 3`

### Banned patterns — body only

| rule_id | Disallowed | Fix |
|---|---|---|
| `OPERATOR_ATTRIBUTION` | `florian asked`, `as requested`, `per request`, `per my request` | Replace with WHY: "fix X because Y" |
| `AGENT_SELF_TALK` | `addressed all`, `fix all`, `fixed all`, `cleaned up everything` | Name specific changes: "fix N+1 in Foo.query, dedupe Bar.helper" |

### Banned patterns — subject only

| rule_id | Disallowed | Fix |
|---|---|---|
| `WEB_UI_DEFAULT` | `Add files via upload`, `Update Foo.md`, `Initial commit` | Use `type(scope): subject`; describe what changed |
| `VERSION_IN_SUBJECT` | Subject starting with `v[0-9]` | Drop the version prefix; use `chore(release): 1.2.3` if needed |

### Exempt subjects (skip the format check entirely)

- Subjects starting with `Merge ` (git merge commits)
- Subjects starting with `Revert ` (`git revert`-generated)
- Subjects starting with `cherry-pick: ` (labeled cherry-picks)
- Subjects starting with `[hotfix] ` (emergency hotfix override)

### Bot allowlist

Commits authored by these identities skip the `WHY_REQUIRED` rule (subject banned-patterns still apply):

- `renovate[bot]`
- `dependabot[bot]` (this repo's `.github/dependabot.yml` sets `commit-message.prefix: "chore"` so the format check passes)
- `pre-commit-ci[bot]`
- `app/github-actions`

### Bypass policy

`git commit --no-verify` skips the local commit-msg hook. CI still validates on push. To pass CI on a sanctioned bypass:

1. Subject matches an exempt prefix (`Merge `, `Revert `, `cherry-pick: `, `[hotfix] `), OR
2. Body includes a `Policy-Override: <reason>` trailer

Example sanctioned bypass:

```bash
git commit --no-verify -m "[hotfix] fix prod outage from migration 0042" \
  -m "" \
  -m "Policy-Override: prod outage; migrating roll-forward fix; full review tomorrow"
```

The pre-push hook logs every `--no-verify` to `~/.commit-bypass.log` with the override reason.

### Where the rules live

- **Canonical spec:** https://github.com/florianhorner/engineering-standards/blob/main/specs/commit-message-spec.md
- **Vendored copy in this repo:** [`.config/commit-rules.json`](.config/commit-rules.json) — SHA-pinned snapshot consumed by the local hook, the commitlint config, and CI. Do not hand-edit.
<!-- END: commit-message-standards -->
