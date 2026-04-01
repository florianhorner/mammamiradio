# HA Addon Release Runbook

How to release a new version of the Mamma Mi Radio Home Assistant addon without breaking anything.

## The release chain

```
Code change
  → bump version in BOTH files (see below)
  → push/merge to main
  → addon-build.yml CI validates + builds images
  → GHCR receives :version and :latest tags
  → HA discovers new version via config.yaml
  → User clicks "Update" in HA
  → HA pulls image from GHCR
  → Container starts with /run.sh
  → run.sh reads /data/options.json → sets env vars
  → config.py reads env vars + radio.toml → builds StationConfig
  → main.py starts producer + streamer
```

Every step must succeed. A break at ANY point means the addon doesn't work.

## Version: two files, must match

| File | Field | Example |
|------|-------|---------|
| `ha-addon/mammamiradio/config.yaml` | `version:` | `1.1.0` |
| `pyproject.toml` | `version =` | `"1.1.0"` |

CI validates they match. If they don't, the build fails.

**How to bump:**
```bash
# Both files, same version, same commit
sed -i '' 's/^version:.*/version: 1.0.3/' ha-addon/mammamiradio/config.yaml
sed -i '' 's/^version = .*/version = "1.0.3"/' pyproject.toml
```

## Config options: the contract

When you add an option to the HA addon configuration UI, you must update THREE files in the same commit:

| File | What to add |
|------|-------------|
| `ha-addon/mammamiradio/config.yaml` | Option in `options:` + type in `schema:` |
| `ha-addon/mammamiradio/rootfs/run.sh` | Key in the Python extraction loop |
| `ha-addon/mammamiradio/translations/en.yaml` | Human-readable name + description |

CI validates that every schema key appears in run.sh. If you add to config.yaml but forget run.sh, the build fails.

The option extraction in run.sh uses a single Python script that reads keys from `/data/options.json` and exports them as env vars with UPPER_CASE names. To add a new option:

1. Add to the `for key in (...)` tuple in run.sh
2. The env var name is `key.upper()` — e.g., `my_option` becomes `MY_OPTION`
3. Read it in `config.py` via `os.getenv("MY_OPTION", "default")`

## Secrets: password type

API keys and secrets use `password` type in the schema (not `str`). This masks them in the HA UI:

```yaml
schema:
  my_api_key: password?
```

## Dockerfile: local source, not GitHub

The addon Dockerfile installs mammamiradio from LOCAL source copied by CI into the build context. It does NOT fetch from GitHub. This means:

- The image always matches the exact commit that triggered the build
- No dependency on GitHub being reachable during Docker build
- No risk of building with stale code from a different branch

CI copies `mammamiradio/`, `pyproject.toml`, and `radio.toml` into `ha-addon/mammamiradio/` before building.

## Image path

HA expects images at:
```
ghcr.io/florianhorner/mammamiradio-addon-{arch}
```

This is set in `ha-addon/mammamiradio/config.yaml` (`image:` field) and must match what `addon-build.yml` pushes to. CI validates this.

The standalone Docker image (for non-HA users) is separate: `ghcr.io/florianhorner/mammamiradio`. Built by `docker.yml` on version tags only.

## Pre-merge checklist

Before merging ANY change that touches addon files:

- [ ] Version bumped in both files (if this is a release)
- [ ] `ruff check . && ruff format --check .` passes
- [ ] `pytest tests/` passes (200+ tests)
- [ ] If new config option: added to config.yaml + run.sh + translations
- [ ] If path changed: grep all files for the old path
- [ ] If renamed anything: `grep -r "old_name" .` returns zero hits

## Post-merge verification

After merging to main, verify the full chain:

1. **CI passed**: Check GitHub Actions for green build
2. **Image exists on GHCR**: `docker pull ghcr.io/florianhorner/mammamiradio-addon-aarch64:VERSION`
3. **Image is public**: Check github.com/florianhorner?tab=packages
4. **HA sees update**: Settings > Add-ons > Mamma Mi Radio > shows new version
5. **Update works**: Click Update, wait for download, check logs
6. **App starts**: Addon log shows "Starting uvicorn on 0.0.0.0:8000..."
7. **Ingress works**: Click addon in sidebar, dashboard loads

Do NOT merge the next PR until all 7 steps pass.

## Common failures

### "An unknown error occurred with addon"
- Check addon logs (Settings > Add-ons > Mamma Mi Radio > Log)
- If "radio.toml not found": image is corrupt, rebuild
- If "model not found": claude_model value doesn't match Anthropic's API
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
| FIFO `/tmp/mammamiradio.pcm` | config.py, radio.toml, go-librespot-config.yml |
| go-librespot port 3678 | config.py, radio.toml, go-librespot-config.yml |
| `/data/go-librespot` | config.py (addon override), Dockerfile (`COPY`) |

If you change any of these, grep for the old value and update all locations.
